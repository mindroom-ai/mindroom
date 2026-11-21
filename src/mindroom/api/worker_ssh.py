"""Start sshd on dedicated workers from the primary's read-only key projection."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from asyncio import create_subprocess_exec
from contextlib import suppress
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request

from mindroom.api.sandbox_exec import runner_dedicated_worker_root
from mindroom.api.sandbox_runner import app_runtime_paths, prepare_terminal_launch, validate_runner_token
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.constants import sandbox_startup_manifest_path
from mindroom.previews.listeners import group_listens
from mindroom.tailnet_ssh import session
from mindroom.terminal.session import TerminalError
from mindroom.workers.models import WORKER_SSH_PORT
from mindroom.workers.protocol import ExecuteRequest  # noqa: TC001 - pydantic reads it at runtime

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter(prefix="/api/sandbox-runner/ssh", tags=["worker-ssh"])
_START_SECONDS = 15.0
_START_FAILED = "The worker's SSH server did not start."


class _WorkerSsh:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.sshd: asyncio.subprocess.Process | None = None


def _worker_ssh(app: FastAPI) -> _WorkerSsh:
    try:
        return app.state.worker_ssh
    except AttributeError:
        state = app.state.worker_ssh = _WorkerSsh()
        return state


def _admits_nobody(projection_dir: Path) -> bool:
    try:
        return not (projection_dir / "authorized_keys").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return True


def _sshd_argv(projection_dir: Path) -> list[str]:
    # Only the worker knows the projection mount and the interpreter for ForceCommand.
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
    """Store the terminal launch for the SSH session module."""
    launch = prepare_terminal_launch(app, payload, None)
    if launch.cwd is None:
        raise TerminalError(400, "This shell has no workspace for an SSH session to start in.")
    content = json.dumps({"env": launch.env, "cwd": launch.cwd}).encode()
    directory = session.LAUNCH_PATH.parent
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # The environment holds what the agent's shell holds, so only the worker's uid reads it.
        atomic_write_bytes_at(directory_fd, session.LAUNCH_PATH.name, content, file_mode=0o600)
    finally:
        os.close(directory_fd)


async def _port_accepts() -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", WORKER_SSH_PORT)
    except OSError:
        return False
    writer.close()
    return True


async def _accepts(sshd: asyncio.subprocess.Process) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_SECONDS
    with suppress(OSError):
        while sshd.returncode is None and loop.time() < deadline:
            if await asyncio.to_thread(group_listens, WORKER_SSH_PORT, sshd.pid):
                return sshd.returncode is None
            await asyncio.sleep(0.1)
    return False


async def _ensure_sshd(app: FastAPI, state: _WorkerSsh) -> None:
    """Ensure sshd listens. The caller holds the lock."""
    root = runner_dedicated_worker_root(app_runtime_paths(app))
    projection_dir = sandbox_startup_manifest_path(root).parent / "ssh"
    if await asyncio.to_thread(_admits_nobody, projection_dir):
        raise HTTPException(409, "No SSH public keys are authorized.")
    if state.sshd is None or state.sshd.returncode is not None:
        await _stop(state)
        if await _port_accepts():
            raise HTTPException(503, _START_FAILED)
        try:
            # Inherit stderr so reaping sshd doesn't wait for a pipe held open by a session.
            state.sshd = await create_subprocess_exec(
                *_sshd_argv(projection_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
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


@router.post("/start", status_code=204, dependencies=[Depends(validate_runner_token)])
async def start(http: Request, request: Annotated[ExecuteRequest, Body(embed=True)]) -> None:
    """Refresh the session launch and confirm sshd is listening."""
    try:
        await asyncio.to_thread(_store_launch, http.app, request)
    except TerminalError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from None
    except OSError:
        raise HTTPException(503, _START_FAILED) from None
    state = _worker_ssh(http.app)
    async with state.lock:
        await _ensure_sshd(http.app, state)
