"""An agent's terminals in the sandbox runner where its shell commands run, reached from the primary."""

from __future__ import annotations

import asyncio
import os
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest
from aiohttp import web
from structlog.testing import capture_logs

from mindroom.agent_cli.shell_contract import AGENT_CLI_TOKEN_ENV, AgentCliShellEnv, bound_agent_cli_shell_env
from mindroom.agents import build_agent_toolkit
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.terminal import client as client_module
from mindroom.terminal import session as session_module
from mindroom.terminal.client import TerminalDetachedError, WorkerTerminals
from mindroom.terminal.lines import PROMPT_COMMAND
from mindroom.terminal.session import TerminalError
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import toolkit_function, toolkit_worker_stand_ins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import tool_placement, worker_call
from mindroom.tool_system.tool_environment import ToolEnvironment, collected_tool_environment
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.terminal.conftest import RUNNER_TOKEN, primary_paths, serve_runner

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.terminal.client import TerminalStream
    from mindroom.tool_system.agno_toolkits import AnyToolkit

pytestmark = pytest.mark.asyncio

# Headers the runner refuses: no token, and a token that is not ASCII.
_REFUSED_TOKEN_HEADERS = ({}, {"x-mindroom-sandbox-token": "runner-tökén"})
_IDENTITY = ToolExecutionIdentity("matrix", "code", "@alice:localhost", "!room:localhost", None, None, "session")


def _shell(paths: RuntimePaths) -> AnyToolkit:
    """Build the agent's worker-routed shell toolkit as a reply to ``_IDENTITY`` builds it."""
    config = Config(agents={"code": AgentConfig(display_name="Code", tools=["shell"])})
    toolkit = build_agent_toolkit(
        "shell",
        agent_tools=resolve_agent_tools(config, "code", builtin_tool_registry()),
        placement=tool_placement(paths, builtin_tool_registry()),
        runtime_paths=paths,
        execution_identity=_IDENTITY,
    )
    assert toolkit is not None
    return toolkit


def _terminals(toolkit: AnyToolkit) -> WorkerTerminals:
    stand_ins = toolkit_worker_stand_ins(toolkit)
    assert stand_ins is not None
    return WorkerTerminals(stand_ins)


async def _run_shell_command(toolkit: AnyToolkit, command: str) -> str:
    function = toolkit_function(toolkit, "run_shell_command")
    assert function is not None
    result = await function.entrypoint(command, tail=1000)
    assert isinstance(result, str)
    return result


async def _read_until(stream: TerminalStream, marker: bytes) -> bytes:
    """Return the output until ``marker``, which a command's echo never holds."""
    seen = b""
    async with asyncio.timeout(20):
        while marker not in seen:
            event = await stream.receive()
            assert event.type == "output", event
            seen += event.data
    return seen


async def _exit_code(stream: TerminalStream) -> int | None:
    async with asyncio.timeout(20):
        while (event := await stream.receive()).type != "exit":
            pass
    return event.code


async def _type_and_read_until_detached(stream: TerminalStream, text: str) -> None:
    """Type ``text``, then take only output until the stream ends; a dropped viewer's stream may end before typing."""
    await stream.send_input(text)
    async with asyncio.timeout(20):
        while True:
            assert (await stream.receive()).type == "output"


def _env(listing: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in listing.splitlines() if "=" in line)


@pytest.mark.parametrize("runner", ["inprocess", "subprocess"], indirect=True)
async def test_a_terminal_starts_in_the_environment_and_workspace_of_the_agent_s_commands(
    tmp_path: Path,
    runner: str,
) -> None:
    """The terminal's shell sees what the agent's shell commands see, whether the runner runs them here or in a child.

    That includes the call's own environment, and no startup file in the shell's home runs.
    """
    toolkit = _shell(primary_paths(tmp_path, runner))
    terminals = _terminals(toolkit)
    home = agent_workspace_root_path(tmp_path / "storage", "code")
    home.mkdir(parents=True, exist_ok=True)
    for startup_file in (".bashrc", ".bash_profile", ".profile"):
        (home / startup_file).write_text("export FROM_STARTUP_FILE=1\n")
    call_environment = ToolEnvironment(
        values={"HOME": str(home), "FROM_THE_CALL": "1"},
        path_prepend=("/opt/from-the-call/bin",),
    )
    with collected_tool_environment(call_environment):
        cwd_line, _, command_listing = (await _run_shell_command(toolkit, "env | sort")).partition("\n")
        info = await terminals.open()
    workspace = cwd_line.removeprefix("[cwd: ").removesuffix("]")

    async with terminals.attach(info.id) as stream:
        await stream.send_input("env | sort > terminal.env; exit\r")
        assert await _exit_code(stream) == 0

    terminal_env = _env((home / "terminal.env").read_text())
    command_env = _env(command_listing)
    assert (terminal_env["HOME"], terminal_env["FROM_THE_CALL"]) == (str(home), "1")
    assert terminal_env["PATH"].startswith("/opt/from-the-call/bin:")
    assert "FROM_STARTUP_FILE" not in terminal_env
    assert terminal_env.pop("TERM") == "xterm-256color"
    # The shell marks its prompts and reads no key bindings or history from the workspace.
    assert terminal_env.pop("PROMPT_COMMAND") == PROMPT_COMMAND
    assert (terminal_env.pop("INPUTRC"), terminal_env.pop("HISTFILE")) == ("/dev/null", "")
    for name in ("TERM", "SHLVL", "_"):
        command_env.pop(name, None)
        terminal_env.pop(name, None)
    assert terminal_env == command_env
    assert terminal_env["MINDROOM_AGENT_WORKSPACE"] == workspace
    assert terminal_env["PWD"] == workspace == str(home.resolve())


async def test_a_terminal_opened_while_a_cli_grant_is_bound_starts_without_it(tmp_path: Path, runner: str) -> None:
    """The response's CLI grant reaches the shell commands it binds, never a terminal, whose shell outlives it."""
    toolkit = _shell(primary_paths(tmp_path, runner))
    terminals = _terminals(toolkit)
    grant = AgentCliShellEnv(api_url="http://primary/cli", token="grant-token", bin_dir="/opt/grant/bin")  # noqa: S106
    with bound_agent_cli_shell_env(grant, window="window"):
        command_env = _env((await _run_shell_command(toolkit, "env")).partition("\n")[2])
        info = await terminals.open()
    async with terminals.attach(info.id) as stream:
        await stream.send_input("env > terminal.env; exit\r")
        assert await _exit_code(stream) == 0

    terminal_env = _env((agent_workspace_root_path(tmp_path / "storage", "code") / "terminal.env").read_text())
    assert command_env[AGENT_CLI_TOKEN_ENV] == "grant-token"
    assert command_env["PATH"].startswith("/opt/grant/bin:")
    assert not [name for name in terminal_env if name.startswith("MINDROOM_AGENT_CLI_")]
    assert "/opt/grant/bin" not in terminal_env["PATH"].split(":")


async def test_a_terminal_keeps_running_between_viewers_and_takes_input_and_resize(tmp_path: Path, runner: str) -> None:
    """A second viewer gets the scrollback, then live output; its keys and size reach the shell, and exit ends it."""
    terminals = _terminals(_shell(primary_paths(tmp_path, runner)))
    info = await terminals.open(cols=80, rows=24)
    async with terminals.attach(info.id) as first:
        await first.send_input("for i in $(seq 1 100000); do echo n$i; sleep 0.05; done\r")
        await _read_until(first, b"n3\r\n")

    async with terminals.attach(info.id) as second:
        scrollback = await second.receive()
        assert scrollback.type == "output"
        assert b"n1\r\n" in scrollback.data
        await _read_until(second, b"\r\n")
        await second.send_input("\x03")
        await second.resize(100, 30)
        await second.send_input("stty size; echo $((2*3))sized\r")
        assert b"30 100" in await _read_until(second, b"6sized")
        await second.send_input("exit 3\r")
        assert await _exit_code(second) == 3
        with pytest.raises(TerminalDetachedError):
            await second.receive()

    [ended] = await terminals.sessions()
    assert (ended.id, ended.alive, ended.exit_code, ended.cols, ended.rows) == (info.id, False, 3, 100, 30)
    await terminals.close(info.id)
    assert await terminals.sessions() == []


async def test_a_viewer_that_falls_too_far_behind_is_told_to_attach_again(
    tmp_path: Path,
    runner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped viewer's stream ends without an exit, while the terminal runs on and a new viewer gets its output."""
    terminals = _terminals(_shell(primary_paths(tmp_path, runner)))
    info = await terminals.open()
    with monkeypatch.context() as limited:
        # Any output at all puts the viewer too far behind.
        limited.setattr(session_module, "_MAX_VIEWER_BACKLOG_BYTES", 0)
        async with terminals.attach(info.id) as dropped:
            with pytest.raises(TerminalDetachedError):
                await _type_and_read_until_detached(dropped, "echo $((6*6))done\r")

    [running] = await terminals.sessions()
    assert running.alive
    async with terminals.attach(info.id) as again:
        await again.send_input("echo $((7*7))alive\r")
        await _read_until(again, b"49alive")


async def test_a_terminal_answers_only_the_agent_and_workspace_it_serves(tmp_path: Path, runner: str) -> None:
    """Another agent's terminals are its own, even where its calls reach the same worker and workspace.

    A session ID names only a session, a terminal starts in the workspace, and a request the runner refuses says why.
    """
    terminals = _terminals(_shell(primary_paths(tmp_path, runner)))
    target = terminals.stand_ins.worker_target
    assert target is not None
    other = WorkerTerminals(replace(terminals.stand_ins, worker_target=replace(target, routing_agent_name="other")))
    info = await terminals.open()

    assert await other.sessions() == []
    for refused in (other.close(info.id), terminals.close("../../execute")):
        with pytest.raises(TerminalError) as hidden:
            await refused
        assert hidden.value.status_code == 404
    for session_id in (info.id, "x/../../execute"):
        with pytest.raises(TerminalError) as hidden:
            async with other.attach(session_id):
                pass
        assert hidden.value.status_code == 404
    for cwd in ("..", "missing"):
        with pytest.raises(TerminalError) as outside:
            await terminals.open(cwd=cwd)
        assert outside.value.status_code == 400
    with pytest.raises(TerminalError) as invalid:
        await terminals.open(cols=0)
    assert invalid.value.status_code == 422
    assert invalid.value.detail.startswith("body.cols: ")

    (agent_workspace_root_path(tmp_path / "storage", "code") / "sub").mkdir()
    inside = await terminals.open(cwd="sub")
    async with terminals.attach(inside.id) as stream:
        await stream.send_input("pwd; echo $((5+5))here\r")
        assert b"/workspace/sub\r\n" in await _read_until(stream, b"10here")
    assert [session.id for session in await terminals.sessions()] == [info.id, inside.id]


async def test_runner_routes_need_the_runner_token_and_a_shell_call(runner: str) -> None:
    """Without the runner token nothing answers; a terminal opens only from a shell call's request.

    A token that is not ASCII is refused like any other wrong one.
    """
    sessions = f"{runner}/api/sandbox-runner/terminal/sessions"
    async with aiohttp.ClientSession() as client:
        for headers in _REFUSED_TOKEN_HEADERS:
            async with client.get(sessions, params={"owner": "x"}, headers=headers) as response:
                assert response.status == 401
        body = {"request": {"protocol": 1, "tool_name": "python", "function_name": "run_python_code"}}
        async with client.post(
            sessions,
            params={"owner": "x"},
            json=body,
            headers={"x-mindroom-sandbox-token": RUNNER_TOKEN},
        ) as response:
            assert response.status == 400
            assert (await response.json())["detail"] == "A terminal opens only with a shell call's request."


async def test_a_terminal_s_stream_needs_the_runner_token_even_with_its_owner(tmp_path: Path, runner: str) -> None:
    """Knowing a terminal's ID and owner is not enough to attach to it without the runner token."""
    terminals = _terminals(_shell(primary_paths(tmp_path, runner)))
    info = await terminals.open()
    call = worker_call(terminals.stand_ins, "run_shell_command", start=False)
    assert call is not None
    stream = f"{call.api_root}/terminal/sessions/{info.id}/io"
    params = {"owner": client_module._owner(call.request)}
    async with aiohttp.ClientSession() as client:
        for headers in _REFUSED_TOKEN_HEADERS:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await client.ws_connect(stream, params=params, headers=headers)
            assert refused.value.status == 403
        async with client.ws_connect(stream, params=params, headers=call.headers) as websocket:
            assert (await websocket.receive()).type == aiohttp.WSMsgType.BINARY


async def test_a_stopping_runner_hangs_up_its_terminals(tmp_path: Path) -> None:
    """Every terminal's shell ends when its runner stops."""
    server, serving, url = await serve_runner(tmp_path, "inprocess")
    try:
        terminals = _terminals(_shell(primary_paths(tmp_path, url)))
        info = await terminals.open()
        async with terminals.attach(info.id) as stream:
            await stream.send_input("echo $$ > shell.pid; echo $((9*9))saved\r")
            await _read_until(stream, b"81saved")
    finally:
        server.should_exit = True
        await serving

    pid = int((agent_workspace_root_path(tmp_path / "storage", "code") / "shell.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


class _BackendApiError(Exception):
    """An error of a worker backend's API client, which is neither a ``RuntimeError`` nor an ``OSError``."""


async def test_an_attached_viewer_keeps_marking_the_worker_used(
    tmp_path: Path,
    runner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While a viewer watches, the client looks the worker up again and again, which marks a dedicated one used.

    A lookup that fails, in whatever way the worker backend fails, is logged and does not stop the next ones.
    """
    monkeypatch.setattr(client_module, "_KEEPALIVE_SECONDS", 0.01)
    lookups: list[bool] = []
    real_worker_call = client_module.worker_call

    def recorded(*args: Any, start: bool) -> object:  # noqa: ANN401
        lookups.append(start)
        # The first keepalive's lookup, after opening's and attaching's.
        if len(lookups) == 3:
            raise _BackendApiError
        return real_worker_call(*args, start=start)

    monkeypatch.setattr(client_module, "worker_call", recorded)
    terminals = _terminals(_shell(primary_paths(tmp_path, runner)))
    info = await terminals.open()
    with capture_logs() as logs:
        async with terminals.attach(info.id):
            async with asyncio.timeout(10):
                while lookups.count(False) < 4:  # noqa: ASYNC110 - lookups run in a thread with no event to await
                    await asyncio.sleep(0.01)

    # Opening starts the worker; attaching and every keepalive only look it up.
    assert lookups[0] is True
    assert set(lookups[1:]) == {False}
    failures = [
        log["log_level"] for log in logs if log["event"] == "Terminal keepalive failed to look up the agent's worker"
    ]
    assert failures == ["warning"]


async def test_a_viewer_whose_runner_goes_silent_is_told_to_attach_again(
    tmp_path: Path,
    runner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner whose connection goes silent without closing, as a vanished one's does, ends the stream in a heartbeat."""

    async def silent(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse(autoping=False)
        await websocket.prepare(request)
        # Take every frame, pings included, and answer none.
        async for _ in websocket:
            pass
        return websocket

    app = web.Application()
    app.router.add_get("/{path:.*}", silent)
    server = web.AppRunner(app, access_log=None)
    await server.setup()
    real_worker_call = client_module.worker_call

    def to_silent_peer(*args: Any, **kwargs: Any) -> object:  # noqa: ANN401
        return replace(real_worker_call(*args, **kwargs), api_root=f"http://127.0.0.1:{server.addresses[0][1]}")

    monkeypatch.setattr(client_module, "worker_call", to_silent_peer)
    monkeypatch.setattr(client_module, "_HEARTBEAT_SECONDS", 0.1)
    terminals = _terminals(_shell(primary_paths(tmp_path, runner)))
    try:
        await web.TCPSite(server, "127.0.0.1", 0).start()
        async with terminals.attach("abc") as stream:
            async with asyncio.timeout(10):
                with pytest.raises(TerminalDetachedError):
                    await stream.receive()
    finally:
        await server.cleanup()


class _StoppedDedicatedWorkers:
    """A dedicated backend whose worker is not running: a lookup finds nothing, and nothing may start one."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def ensure_worker(self, spec: object, *, progress_sink: object = None) -> object:
        raise AssertionError(spec, progress_sink)

    def touch_worker(self, worker_key: str) -> None:
        self.touched.append(worker_key)


async def test_a_stopped_dedicated_worker_has_no_terminals_and_is_not_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing, attaching and closing look a dedicated worker up without starting it, and find no terminals."""
    workers = _StoppedDedicatedWorkers()
    monkeypatch.setattr(sandbox_proxy, "lease_primary_worker_manager", lambda *_args, **_kwargs: nullcontext(workers))
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_TOKEN": RUNNER_TOKEN, "MINDROOM_WORKER_BACKEND": "kubernetes"},
    )
    terminals = _terminals(_shell(paths))

    assert await terminals.sessions() == []
    with pytest.raises(TerminalError) as closed:
        await terminals.close("abc")
    assert closed.value.status_code == 404
    with pytest.raises(TerminalError) as attached:
        async with terminals.attach("abc"):
            pass
    assert attached.value.status_code == 404
    assert len(workers.touched) == 3
