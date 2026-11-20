"""Runner route that starts a dedicated worker's SSH server, which tailnet SSH connections reach.

The primary calls ``/start`` for every SSH connection it has admitted to this worker, with the request a shell call
would send. The runner prepares that request as a terminal's (``prepare_terminal_launch``) and keeps the shell's
environment and workspace where ``mindroom.tailnet_ssh.session``, which sshd runs for every session, reads them. Then
it starts sshd unless one runs, on the files the primary wrote into ``<worker root>/.runtime/ssh``.

sshd never starts at boot, so a worker has no SSH server until the primary has passed a connect for it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

from mindroom.api.sandbox_exec import runner_dedicated_worker_root
from mindroom.api.sandbox_runner import app_runtime_paths, prepare_terminal_launch, validate_runner_token
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.constants import sandbox_startup_manifest_path
from mindroom.tailnet_ssh import session
from mindroom.terminal.session import TerminalError
from mindroom.workers.models import WORKER_SSH_PORT
from mindroom.workers.protocol import ExecuteRequest  # noqa: TC001 - pydantic reads it at runtime

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

router = APIRouter(prefix="/api/sandbox-runner/ssh", tags=["worker-ssh"])
# How long a start waits for sshd to accept on its port, and for the last of what an sshd that failed to start wrote.
_START_SECONDS = 15.0
_STDERR_SECONDS = 1.0
_STDERR_TAIL_BYTES = 4096


class _Start(BaseModel):
    request: ExecuteRequest


class _StderrTail(asyncio.Protocol):
    """The end of what sshd wrote to stderr, where ``-e`` sends its log, and whether this runner's end has closed."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = asyncio.Event()

    def data_received(self, data: bytes) -> None:
        """Keep the last ``_STDERR_TAIL_BYTES``."""
        self.data += data
        del self.data[:-_STDERR_TAIL_BYTES]

    def connection_lost(self, exc: Exception | None) -> None:  # noqa: ARG002
        """Note the end: sshd and its sessions closed the pipe, or this runner did."""
        self.closed.set()


@dataclass(eq=False)
class _Sshd:
    """A started sshd, and this runner's end of the pipe its stderr is."""

    process: asyncio.subprocess.Process
    stderr: asyncio.ReadTransport
    log: _StderrTail


class _WorkerSsh:
    """A runner's sshd, if a start has run one, and the lock its starts take turns under."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.sshd: _Sshd | None = None


def _worker_ssh(app: FastAPI) -> _WorkerSsh:
    """Return this runner's SSH server state, created on first use."""
    try:
        return app.state.worker_ssh
    except AttributeError:
        state = app.state.worker_ssh = _WorkerSsh()
        return state


def _sshd_argv(projection_dir: Path) -> list[str]:
    """Return the sshd command that serves from the files the primary wrote into ``projection_dir``.

    Only the worker knows where that folder is mounted and which interpreter it runs, so the paths and the session
    program are options here and ``sshd_config`` names none (``mindroom.tailnet_ssh.projection``).
    """
    return [
        "/usr/sbin/sshd",
        "-D",
        "-e",
        "-f",
        f"{projection_dir}/sshd_config",
        "-o",
        f"HostKey={projection_dir}/ssh_host_ed25519_key",
        "-o",
        f"AuthorizedKeysFile={projection_dir}/authorized_keys",
        "-o",
        f"ForceCommand={sys.executable} -m mindroom.tailnet_ssh.session",
    ]


def _store_launch(app: FastAPI, payload: ExecuteRequest) -> None:
    """Prepare the session's shell as a terminal's and keep it where the session module reads it. Blocking."""
    launch = prepare_terminal_launch(app, payload, None)
    if launch.cwd is None:
        raise TerminalError(400, "This shell has no workspace for an SSH session to start in.")
    content = json.dumps({"argv": list(launch.argv), "env": launch.env, "cwd": launch.cwd}).encode()
    directory = session.LAUNCH_PATH.parent
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # The environment holds what the agent's shell holds, so only the worker's uid reads it.
        atomic_write_bytes_at(directory_fd, session.LAUNCH_PATH.name, content, file_mode=0o600)
    finally:
        os.close(directory_fd)


async def _spawn(argv: list[str], stderr: BinaryIO) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=stderr,
    )


async def _run(argv: list[str]) -> _Sshd:
    """Run sshd with its stderr on a pipe that this runner reads, so that sshd never blocks writing its log.

    The pipe is this module's, not the subprocess transport's: the sessions sshd starts hold it open after sshd, and
    this runner must be able to close its end without waiting for them, as it could not close the transport's.

    Nothing is awaited between sshd's start and this function's return, so a cancelled caller either has no sshd,
    which a cancelled spawn kills itself, or gets the one it then keeps track of.
    """
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb", buffering=0) as theirs:
        log = _StderrTail()
        # Closes the read end itself when it fails.
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: log,
            os.fdopen(read_fd, "rb", buffering=0),
        )
        try:
            process = await _spawn(argv, theirs)
        except BaseException:
            transport.close()
            raise
    return _Sshd(process, transport, log)


async def _end(sshd: _Sshd, *, read_seconds: float = 0) -> None:
    """Kill sshd if it runs and close its stderr, after ``read_seconds`` at most for the last of what it wrote.

    The sessions it started run on, as they do when sshd ends by itself. They hold its stderr, so this does not
    wait for it to close; sshd ignores a log line it cannot write. Ending an sshd again does nothing.
    """
    with suppress(ProcessLookupError):
        sshd.process.kill()
    await sshd.process.wait()
    with suppress(TimeoutError):
        async with asyncio.timeout(read_seconds):
            await sshd.log.closed.wait()
    sshd.stderr.close()
    await sshd.log.closed.wait()


async def _accepts(sshd: _Sshd) -> bool:
    """Return whether sshd accepts on its port within ``_START_SECONDS``; False at once when it has exited."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_SECONDS
    while sshd.process.returncode is None and loop.time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", WORKER_SSH_PORT)
        except OSError:
            await asyncio.sleep(0.1)
        else:
            writer.close()
            return True
    return False


async def _ensure_sshd(app: FastAPI, state: _WorkerSsh) -> None:
    """Start sshd unless it runs, and return once its port accepts. The caller holds the lock."""
    sshd = state.sshd
    if sshd is None or sshd.process.returncode is not None:
        if sshd is not None:
            # An sshd that died: its sessions may run on, and nothing reads its stderr once it is replaced.
            await _end(sshd)
        root = runner_dedicated_worker_root(app_runtime_paths(app))
        try:
            sshd = state.sshd = await _run(_sshd_argv(sandbox_startup_manifest_path(root).parent / "ssh"))
        except FileNotFoundError:
            raise HTTPException(503, "The worker image has no SSH server.") from None
    if await _accepts(sshd):
        return
    await _end(sshd, read_seconds=_STDERR_SECONDS)
    state.sshd = None
    lines = sshd.log.data.decode(errors="replace").split("\n")
    reason = next((line.strip() for line in reversed(lines) if line.strip()), "")
    detail = "The worker's SSH server did not start"
    raise HTTPException(503, f"{detail}: {reason}" if reason else f"{detail}.")


async def stop_worker_ssh(app: FastAPI) -> None:
    """End this runner's sshd as the runner stops."""
    state = _worker_ssh(app)
    async with state.lock:
        if state.sshd is not None:
            await _end(state.sshd)
            state.sshd = None


@router.post("/start", dependencies=[Depends(validate_runner_token)])
async def start(request: Request, payload: _Start) -> dict[str, int]:
    """Keep the session's shell from a shell call's request, start sshd unless it runs, and name its port."""
    try:
        await asyncio.to_thread(_store_launch, request.app, payload.request)
    except TerminalError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from None
    state = _worker_ssh(request.app)
    async with state.lock:
        await _ensure_sshd(request.app, state)
    return {"port": WORKER_SSH_PORT}
