"""The runner's SSH start and stop routes, and the session sshd runs for every connection."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
import signal
import socket
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from mindroom.api import sandbox_runner, sandbox_runner_app, worker_ssh
from mindroom.constants import resolve_runtime_paths
from mindroom.tailnet_ssh import session
from mindroom.terminal.session import TerminalLaunch
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence

_TOKEN = "runner-token"  # noqa: S105
_SHELL_REQUEST = {"protocol": WORKER_PROTOCOL_VERSION, "tool_name": "shell", "function_name": "run_shell_command"}
_BODY = {"request": _SHELL_REQUEST}
# What the shell toolkit returns for a terminal: an interactive Bash that reads no startup file.
_LAUNCH = {
    "argv": ["bash", "--noprofile", "--norc", "-i"],
    "env": {
        "PATH": "/x",
        "PROMPT_COMMAND": "p",
        "INPUTRC": "/dev/null",
        "TERM": "dumb",
        "HTTPS_PROXY": "http://proxy",
    },
    "cwd": "/data/ws",
}
# A stand-in for sshd gets the descriptor of the socket bound to the port the runner probes. This one listens on it.
_LISTENS = """
import socket, sys
server = socket.socket(fileno=int(sys.argv[1]))
server.listen()
while True:
    server.accept()[0].close()
"""
# Starts a session that outlives the listener and inherits its stderr.
_LISTENS_WITH_A_SESSION = f"""
import os, pathlib, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pathlib.Path(sys.argv[2], str(os.getpid())).write_text(str(child.pid))
{_LISTENS}
"""


def test_session_interactive_is_bash_in_workspace() -> None:
    """A login gets Bash at home in the workspace, with the agent's environment and sshd's terminal.

    Not as a login shell, whose ``/etc/profile`` replaces the launch's ``PATH``.
    """
    argv, env, cwd = session._session_command(_LAUNCH, {"TERM": "xterm", "SSH_CONNECTION": "c"})

    assert argv == ["bash"]
    assert cwd == "/data/ws"
    assert env["PATH"] == _LAUNCH["env"]["PATH"]
    assert env == {
        "PATH": "/x",
        "HTTPS_PROXY": "http://proxy",
        "HOME": "/data/ws",
        "USER": "mindroom",
        "LOGNAME": "mindroom",
        "SHELL": "/bin/bash",
        "TERM": "xterm",
        "SSH_CONNECTION": "c",
    }


def test_session_command_runs_through_bash_c() -> None:
    """A command the client names, as rsync and scp do, runs in Bash with the launch's own terminal type."""
    argv, env, cwd = session._session_command(_LAUNCH, {"SSH_ORIGINAL_COMMAND": "rsync --server ."})

    assert argv == ["bash", "-c", "rsync --server ."]
    assert (cwd, env["HOME"], env["TERM"]) == ("/data/ws", "/data/ws", "dumb")
    assert "SSH_ORIGINAL_COMMAND" not in env


def test_session_sftp_runs_the_sftp_server() -> None:
    """The SFTP subsystem, which sshd_config names internal-sftp, is the image's SFTP server."""
    argv, _, cwd = session._session_command(_LAUNCH, {"SSH_ORIGINAL_COMMAND": "internal-sftp"})

    assert argv == ["/usr/lib/openssh/sftp-server"]
    assert cwd == "/data/ws"


def test_session_module_imports_only_stdlib() -> None:
    """Every SSH session starts the module, so it imports nothing of MindRoom, nor anything outside the standard library."""
    tree = ast.parse(Path(session.__file__).read_text(encoding="utf-8"))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""])
    }

    assert imported
    assert imported <= sys.stdlib_module_names


def test_session_main_runs_the_stored_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_main`` reads the launch file, enters its directory, and becomes the session's program."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launch_path = tmp_path / "launch.json"
    launch_path.write_text(json.dumps({**_LAUNCH, "cwd": str(workspace)}), encoding="utf-8")
    monkeypatch.setattr(session, "LAUNCH_PATH", launch_path)
    monkeypatch.setattr(session.os, "environ", {"SSH_ORIGINAL_COMMAND": "true"})
    monkeypatch.chdir(tmp_path)
    executed: list[tuple[str, list[str], dict[str, str], str]] = []
    monkeypatch.setattr(
        session.os,
        "execvpe",
        lambda file, argv, env: executed.append((file, argv, env, str(Path.cwd()))),
    )

    session._main()

    [(file, argv, env, cwd)] = executed
    assert (file, argv, cwd) == ("bash", ["bash", "-c", "true"], str(workspace))
    assert env["HOME"] == str(workspace)


class _FakeSshd:
    """Stands in for sshd: each spawn records its command and runs ``program`` with a socket reserved for it.

    Each spawn gets a socket of its own, bound to a new ``port``, which the runner then probes. The socket does not
    listen, so the port refuses connections until that spawn's program listens on it, and nothing else can take the
    port meanwhile.
    """

    def __init__(self, program: Sequence[str], monkeypatch: pytest.MonkeyPatch, sockets: contextlib.ExitStack) -> None:
        self.program = program
        self.port: int | None = None
        self.argvs: list[list[str]] = []
        self.processes: list[asyncio.subprocess.Process] = []
        self._monkeypatch = monkeypatch
        self._sockets = sockets

    async def spawn(self, argv: list[str]) -> asyncio.subprocess.Process:
        self.argvs.append(argv)
        reserved = self._sockets.enter_context(socket.socket())
        reserved.bind(("127.0.0.1", 0))
        self.port = reserved.getsockname()[1]
        self._monkeypatch.setattr(worker_ssh, "WORKER_SSH_PORT", self.port)
        descriptor = reserved.fileno()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            self.program[0],
            str(descriptor),
            *self.program[1:],
            stdin=asyncio.subprocess.DEVNULL,
            pass_fds=(descriptor,),
        )
        self.processes.append(process)
        return process


async def _exited(process: asyncio.subprocess.Process) -> None:
    """Wait until ``process`` has exited, though a session it started may still hold its stderr."""
    async with asyncio.timeout(10):
        while process.returncode is None:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


@pytest.fixture
def install_fake_sshd(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., _FakeSshd]]:
    """Make the runner spawn a stand-in program instead of sshd, and probe the port reserved for each spawn."""
    with contextlib.ExitStack() as sockets:

        def install(*program: str) -> _FakeSshd:
            fake = _FakeSshd(program, monkeypatch, sockets)
            monkeypatch.setattr(worker_ssh, "_spawn", fake.spawn)
            return fake

        yield install


@pytest.fixture
def fake_sshd(install_fake_sshd: Callable[..., _FakeSshd]) -> _FakeSshd:
    """A stand-in sshd that listens."""
    return install_fake_sshd(_LISTENS)


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    """Project a key before an admitted start."""
    path = tmp_path / "worker" / ".runtime" / "ssh" / "authorized_keys"
    path.parent.mkdir(parents=True)
    path.write_text("restrict,pty ssh-ed25519 AAAA alice@laptop\n", encoding="utf-8")
    return path


@pytest_asyncio.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Call the runner of the dedicated worker at ``tmp_path/worker``, whose shell launch is ``_LAUNCH``."""
    assert keys.is_file()
    app = sandbox_runner_app.app
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_SANDBOX_RUNNER_MODE": "true",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(tmp_path / "worker"),
        },
    )
    sandbox_runner.initialize_sandbox_runner_app(app, paths, runner_token=_TOKEN)
    monkeypatch.setattr(session, "LAUNCH_PATH", tmp_path / "mindroom-ssh" / "launch.json")
    launch = TerminalLaunch(argv=tuple(_LAUNCH["argv"]), env=dict(_LAUNCH["env"]), cwd=_LAUNCH["cwd"])
    monkeypatch.setattr(worker_ssh, "prepare_terminal_launch", lambda _app, _payload, _cwd: launch)
    transport = httpx.ASGITransport(app=app)
    headers = {"x-mindroom-sandbox-token": _TOKEN}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner", headers=headers) as runner:
            yield runner
    finally:
        await worker_ssh.stop_worker_ssh(app)
        del app.state.worker_ssh


@pytest.mark.asyncio
async def test_start_requires_runner_token(client: httpx.AsyncClient, fake_sshd: _FakeSshd) -> None:
    """Only the primary, which holds the runner token, starts sshd."""
    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY, headers={"x-mindroom-sandbox-token": ""})

    assert response.status_code == 401
    assert fake_sshd.argvs == []
    assert not session.LAUNCH_PATH.exists()


@pytest.mark.asyncio
async def test_start_writes_launch_0600_and_spawns_sshd(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    tmp_path: Path,
) -> None:
    """A start keeps the shell's launch for the session module and runs sshd on the projection the primary wrote."""
    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert (response.status_code, response.json()) == (200, {"port": fake_sshd.port})
    assert stat.S_IMODE(session.LAUNCH_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(session.LAUNCH_PATH.parent.stat().st_mode) == 0o700
    assert [entry.name for entry in session.LAUNCH_PATH.parent.iterdir()] == ["launch.json"]
    assert json.loads(session.LAUNCH_PATH.read_text(encoding="utf-8")) == _LAUNCH
    projection = tmp_path / "worker" / ".runtime" / "ssh"
    assert fake_sshd.argvs == [worker_ssh._sshd_argv(projection)]
    assert fake_sshd.argvs[0] == [
        "/usr/sbin/sshd",
        "-D",
        "-e",
        "-f",
        f"{projection}/sshd_config",
        "-o",
        f"HostKey={projection}/ssh_host_ed25519_key",
        "-o",
        f"AuthorizedKeysFile={projection}/authorized_keys",
        "-o",
        f"ForceCommand={sys.executable} -m mindroom.tailnet_ssh.session",
    ]


@pytest.mark.asyncio
async def test_start_keeps_the_latest_launch_while_sshd_runs(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every start stores its launch, so a session gets the environment of the connect that admitted it."""
    await client.post("/api/sandbox-runner/ssh/start", json=_BODY)
    later = TerminalLaunch(argv=("bash",), env={"PATH": "/later"}, cwd="/data/ws")
    monkeypatch.setattr(worker_ssh, "prepare_terminal_launch", lambda _app, _payload, _cwd: later)

    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert response.status_code == 200
    assert json.loads(session.LAUNCH_PATH.read_text(encoding="utf-8"))["env"] == {"PATH": "/later"}
    assert len(fake_sshd.argvs) == 1


@pytest.mark.asyncio
async def test_start_503_when_image_has_no_sshd(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn failure returns a generic error, without exposing paths."""
    monkeypatch.setattr(worker_ssh, "_sshd_argv", lambda _projection: [str(tmp_path / "usr" / "sbin" / "sshd"), "-D"])

    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert (response.status_code, response.json()) == (503, {"detail": "The worker's SSH server did not start."})


@pytest.mark.asyncio
async def test_concurrent_starts_spawn_one_sshd(client: httpx.AsyncClient, fake_sshd: _FakeSshd) -> None:
    """Connections that arrive together share one sshd."""
    responses = await asyncio.gather(
        client.post("/api/sandbox-runner/ssh/start", json=_BODY),
        client.post("/api/sandbox-runner/ssh/start", json=_BODY),
    )

    assert [response.status_code for response in responses] == [200, 200]
    assert len(fake_sshd.argvs) == 1


@pytest.mark.asyncio
async def test_start_restarts_sshd_that_exited(client: httpx.AsyncClient, fake_sshd: _FakeSshd) -> None:
    """A start after sshd died runs a new one, and answers once that one listens."""
    await client.post("/api/sandbox-runner/ssh/start", json=_BODY)
    fake_sshd.processes[0].kill()
    await _exited(fake_sshd.processes[0])

    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    # The port is the second stand-in's own, which only it opens.
    assert (response.status_code, response.json()) == (200, {"port": fake_sshd.port})
    assert len(fake_sshd.argvs) == 2
    assert fake_sshd.processes[1].returncode is None


@pytest.mark.asyncio
async def test_start_does_not_take_a_dead_sshd_s_port_for_its_replacement(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement that never listens is not ready, though the sshd before it listened."""
    await client.post("/api/sandbox-runner/ssh/start", json=_BODY)
    fake_sshd.processes[0].kill()
    await _exited(fake_sshd.processes[0])
    fake_sshd.program = ["import time; time.sleep(60)"]
    monkeypatch.setattr(worker_ssh, "_START_SECONDS", 0.3)

    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert (response.status_code, response.json()) == (503, {"detail": "The worker's SSH server did not start."})
    assert fake_sshd.processes[1].returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_shutdown_stops_the_sshd_of_a_start_cancelled_right_after_spawning_it(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that is cancelled once sshd runs leaves no sshd the runner has lost track of."""

    async def spawn_and_cancel(argv: list[str]) -> asyncio.subprocess.Process:
        process = await fake_sshd.spawn(argv)
        task = asyncio.current_task()
        assert task is not None
        # Takes effect at the start's next await.
        task.cancel()
        return process

    monkeypatch.setattr(worker_ssh, "_spawn", spawn_and_cancel)
    start = asyncio.create_task(client.post("/api/sandbox-runner/ssh/start", json=_BODY))
    with pytest.raises(asyncio.CancelledError):
        await start

    await worker_ssh.stop_worker_ssh(sandbox_runner_app.app)

    [process] = fake_sshd.processes
    try:
        assert process.returncode == -signal.SIGKILL
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_start_does_not_expose_sshd_stderr_when_it_exits_at_once(
    client: httpx.AsyncClient,
    install_fake_sshd: Callable[..., _FakeSshd],
) -> None:
    """When sshd refuses its files, the start fails without exposing stderr or waiting for the port."""
    exits = (
        "import sys; print('reading the key', file=sys.stderr); print('bad ownership\\n', file=sys.stderr); sys.exit(1)"
    )
    fake = install_fake_sshd(exits)

    async with asyncio.timeout(10):
        response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert response.status_code == 503
    assert response.json() == {"detail": "The worker's SSH server did not start."}
    # The next connect tries again.
    await client.post("/api/sandbox-runner/ssh/start", json=_BODY)
    assert len(fake.argvs) == 2


@pytest.mark.asyncio
async def test_start_stops_sshd_that_never_listens(
    client: httpx.AsyncClient,
    install_fake_sshd: Callable[..., _FakeSshd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When sshd does not take its port in time it is stopped, so that the next connect starts a new one."""
    fake = install_fake_sshd("import time; time.sleep(60)")
    monkeypatch.setattr(worker_ssh, "_START_SECONDS", 0.3)

    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert (response.status_code, response.json()) == (503, {"detail": "The worker's SSH server did not start."})
    assert fake.processes[0].returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_start_refuses_a_non_shell_request(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session's shell comes only from a shell call's request, as a terminal's does."""
    monkeypatch.setattr(worker_ssh, "prepare_terminal_launch", sandbox_runner.prepare_terminal_launch)

    response = await client.post(
        "/api/sandbox-runner/ssh/start",
        json={"request": {**_SHELL_REQUEST, "tool_name": "file"}},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "A terminal opens only with a shell call's request."}
    assert fake_sshd.argvs == []
    assert not session.LAUNCH_PATH.exists()


@pytest.mark.asyncio
async def test_start_refuses_a_shell_without_a_workspace(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session's home is the agent's workspace, so a shell that has none gets no session."""
    homeless = TerminalLaunch(argv=("bash",), env={}, cwd=None)
    monkeypatch.setattr(worker_ssh, "prepare_terminal_launch", lambda _app, _payload, _cwd: homeless)

    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert response.status_code == 400
    assert response.json() == {"detail": "This shell has no workspace for an SSH session to start in."}
    assert fake_sshd.argvs == []


@pytest.mark.asyncio
async def test_stop_route_kills_sshd_and_is_idempotent(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    keys: Path,
) -> None:
    """A stop ends sshd, and answers the same when none runs; the next start runs a new one."""
    assert (await client.post("/api/sandbox-runner/ssh/stop")).status_code == 204
    await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    original = keys.read_text(encoding="utf-8")
    keys.write_text("", encoding="utf-8")
    responses = [await client.post("/api/sandbox-runner/ssh/stop") for _ in range(2)]
    keys.write_text(original, encoding="utf-8")

    assert [response.status_code for response in responses] == [204, 204]
    assert fake_sshd.processes[0].returncode == -signal.SIGKILL
    assert (await client.post("/api/sandbox-runner/ssh/start", json=_BODY)).status_code == 200
    assert len(fake_sshd.argvs) == 2
    assert fake_sshd.processes[1].returncode is None


@pytest.mark.asyncio
async def test_a_stop_sent_before_a_newer_connect_leaves_that_connects_sshd(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    keys: Path,
) -> None:
    """The primary cleared the keys and sent a stop; a connect wrote keys and started sshd before the stop arrived.

    The stop then finds keys that admit someone, so the newer connect's sshd runs on. Once the keys are empty again,
    as the next removal leaves them, a stop ends it.
    """
    await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    response = await client.post("/api/sandbox-runner/ssh/stop")

    assert response.status_code == 204
    assert fake_sshd.processes[0].returncode is None
    keys.write_text("", encoding="utf-8")
    assert (await client.post("/api/sandbox-runner/ssh/stop")).status_code == 204
    assert fake_sshd.processes[0].returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_stop_route_requires_runner_token(client: httpx.AsyncClient, fake_sshd: _FakeSshd) -> None:
    """Only the primary, which holds the runner token, stops sshd."""
    await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    response = await client.post("/api/sandbox-runner/ssh/stop", headers={"x-mindroom-sandbox-token": ""})

    assert response.status_code == 401
    assert fake_sshd.processes[0].returncode is None


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_start_refuses_missing_or_empty_keys(
    client: httpx.AsyncClient,
    fake_sshd: _FakeSshd,
    keys: Path,
    *,
    present: bool,
) -> None:
    """A delayed start must not launch a listener after the primary clears its keys."""
    if present:
        keys.write_text("", encoding="utf-8")
    else:
        keys.unlink()
    response = await client.post("/api/sandbox-runner/ssh/start", json=_BODY)

    assert response.status_code == 409
    assert not fake_sshd.argvs


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "shutdown", "restart"])
async def test_listener_cleanup_does_not_wait_for_sessions(
    client: httpx.AsyncClient,
    tmp_path: Path,
    install_fake_sshd: Callable[..., _FakeSshd],
    keys: Path,
    operation: str,
) -> None:
    """The listener is reaped promptly even if a session inherited its stderr."""
    fake = install_fake_sshd(_LISTENS_WITH_A_SESSION, str(tmp_path))
    app = sandbox_runner_app.app
    try:
        async with asyncio.timeout(5), app.router.lifespan_context(app):
            assert (await client.post("/api/sandbox-runner/ssh/start", json=_BODY)).status_code == 200
            listener = fake.processes[0]
            if operation == "stop":
                keys.write_text("", encoding="utf-8")
                assert (await client.post("/api/sandbox-runner/ssh/stop")).status_code == 204
            elif operation == "restart":
                listener.kill()
                await _exited(listener)
                assert (await client.post("/api/sandbox-runner/ssh/start", json=_BODY)).status_code == 200
                assert fake.processes[1].returncode is None
        assert listener.returncode == -signal.SIGKILL
        os.kill(int((tmp_path / str(listener.pid)).read_text()), 0)
    finally:
        for process in fake.processes:
            marker = tmp_path / str(process.pid)
            if marker.exists():
                os.kill(int(marker.read_text()), signal.SIGKILL)
