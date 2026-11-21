"""Runner routes that start and stop a dedicated worker's SSH server, which tailnet SSH connections reach.

The primary calls ``/start`` for every SSH connection it has admitted to this worker, with the request a shell call
would send. The runner prepares that request as a terminal's (``prepare_terminal_launch``) and keeps the shell's
environment and workspace where ``mindroom.tailnet_ssh.session``, which sshd runs for every session, reads them. Then
it starts sshd unless one runs, on the files the primary wrote into ``<worker root>/.runtime/ssh``.

sshd never starts at boot, so a worker has no SSH server until the primary has passed a connect for it. The primary
calls ``/stop`` when it removes the worker's link, after it has emptied ``authorized_keys``; sshd ends only while that
file still admits nobody, so a stop that arrives after a newer connect wrote keys and started sshd leaves it running.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
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

router = APIRouter(prefix="/api/sandbox-runner/ssh", tags=["worker-ssh"])
_START_SECONDS = 15.0
_START_FAILED = "The worker's SSH server did not start."


class _Start(BaseModel):
    request: ExecuteRequest


class _WorkerSsh:
    """A runner's sshd, if a start has run one, and the lock its starts take turns under."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.sshd: asyncio.subprocess.Process | None = None


def _worker_ssh(app: FastAPI) -> _WorkerSsh:
    """Return this runner's SSH server state, created on first use."""
    try:
        return app.state.worker_ssh
    except AttributeError:
        state = app.state.worker_ssh = _WorkerSsh()
        return state


def _projection_dir(app: FastAPI) -> Path:
    """Return where this worker mounts the files the primary writes for its sshd."""
    return sandbox_startup_manifest_path(runner_dedicated_worker_root(app_runtime_paths(app))).parent / "ssh"


def _admits_nobody(projection_dir: Path) -> bool:
    """Return whether ``authorized_keys`` holds no key, or is not there. Blocking."""
    try:
        return not (projection_dir / "authorized_keys").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return True


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


async def _spawn(argv: list[str]) -> asyncio.subprocess.Process:
    # Inherit stderr: sessions can outlive the listener and must not keep its reaper waiting on a pipe.
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
    )


async def _accepts(sshd: asyncio.subprocess.Process) -> bool:
    """Return whether sshd accepts on its port within ``_START_SECONDS``; False at once when it has exited."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_SECONDS
    while sshd.returncode is None and loop.time() < deadline:
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
    projection_dir = _projection_dir(app)
    if await asyncio.to_thread(_admits_nobody, projection_dir):
        raise HTTPException(409, "No SSH public keys are authorized.")
    if state.sshd is None or state.sshd.returncode is not None:
        try:
            state.sshd = await _spawn(_sshd_argv(projection_dir))
        except OSError:
            raise HTTPException(503, _START_FAILED) from None
    if not await _accepts(state.sshd):
        await _stop(state)
        raise HTTPException(503, _START_FAILED)


async def _stop(state: _WorkerSsh) -> None:
    """End sshd if it runs; its open sessions are not waited for. The caller holds the lock."""
    if state.sshd is not None:
        with suppress(ProcessLookupError):
            state.sshd.kill()
        await state.sshd.wait()
        state.sshd = None


async def stop_worker_ssh(app: FastAPI) -> None:
    """End this runner's sshd as the runner stops."""
    state = _worker_ssh(app)
    async with state.lock:
        await _stop(state)


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


@router.post("/stop", status_code=204, dependencies=[Depends(validate_runner_token)])
async def stop(request: Request) -> None:
    """End sshd if it runs and its ``authorized_keys`` admits nobody, as the primary leaves it for a removed link.

    Starts take the same lock, and every start follows the keys its connect wrote, so a stop sent before a newer
    connect never ends that connect's sshd. The gateway has closed the removed link's sessions already.
    """
    state = _worker_ssh(request.app)
    async with state.lock:
        if await asyncio.to_thread(_admits_nobody, _projection_dir(request.app)):
            await _stop(state)
