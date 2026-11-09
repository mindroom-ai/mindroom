"""Runner routes that start, report, and stop the services plugins run in this dedicated worker.

A service is a program in the plugin's folder, which the request's config places as it does for ``/execute``, started as
a tool's subprocess is: with the worker's Python and sanitized environment, in a process group of its own. It serves on
the loopback port in ``MINDROOM_WORKER_SERVICE_PORT``, ``MINDROOM_WORKER_SERVICE_RUNNER_URL`` is this runner's URL, and
``MINDROOM_AGENT_BROWSER_URL`` is its bridge to the agent's browser page.
Every process in the worker can reach that port, so each start gets a fresh secret on stdin, a socket that other
processes cannot open through ``/proc`` as they could a pipe, or read as they read the environment and command line;
the program refuses requests whose ``X-MindRoom-Service-Secret`` header lacks it, and the secret never leaves this runner.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import signal
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request
from pydantic import Field

from mindroom.api.sandbox_exec import runner_uses_dedicated_worker, worker_subprocess_env
from mindroom.api.sandbox_runner import app_runtime_paths, prepare_route, validate_runner_token
from mindroom.api.worker_preview import loopback_host
from mindroom.plugin_worker_services import SERVICE_NAME_PATTERN, WorkerServiceState
from mindroom.tool_system.plugins import refresh_plugins
from mindroom.workers.protocol import WorkerFailure, WorkerRoute

if TYPE_CHECKING:
    from pathlib import Path as FilePath

router = APIRouter(
    prefix="/api/sandbox-runner/plugin-services",
    tags=["sandbox-runner"],
    dependencies=[Depends(validate_runner_token)],
)
_Name = Annotated[str, Path(pattern=f"^{SERVICE_NAME_PATTERN}$")]
# How long an ensure waits for a new instance to listen, and a stopped one's process group gets before it is killed.
_START_SECONDS = 10.0
_STOP_SECONDS = 5.0


class _Ensure(WorkerRoute):
    module: str = Field(min_length=1, max_length=4096)
    idle_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


@dataclass(frozen=True)
class _Program:
    path: FilePath
    entry_hash: str
    python: str
    env: dict[str, str] = field(repr=False)
    cwd: str
    idle_timeout_seconds: float


@dataclass(eq=False)
class _ServiceInstance:
    """One service's process, which leads its own process group, with its port, secret, and program's hash."""

    process: asyncio.subprocess.Process
    port: int
    secret: str = field(repr=False)
    entry_hash: str
    idle_timeout_seconds: float
    idle_until: float = 0.0
    expiry: asyncio.Task[None] | None = None


class _WorkerServices:
    """A runner's service instances by plugin and service name: one each, kept until a stop or its idle timeout."""

    def __init__(self) -> None:
        self._instances: dict[tuple[str, str], _ServiceInstance] = {}
        self._lock = asyncio.Lock()
        # How many stops each service has had, so a start that a stop arrived after refuses to start it.
        self._stops: dict[tuple[str, str], int] = {}

    def stops(self, plugin: str, name: str) -> int:
        """Return how many stops the service has had."""
        return self._stops.get((plugin, name), 0)

    def instance(self, plugin: str, name: str) -> _ServiceInstance | None:
        """Return the service's instance, running or exited, or None."""
        return self._instances.get((plugin, name))

    def touch(self, plugin: str, name: str) -> None:
        """Reset the instance's idle timer."""
        if (instance := self.instance(plugin, name)) is not None:
            instance.idle_until = asyncio.get_running_loop().time() + instance.idle_timeout_seconds

    async def ensure(
        self,
        plugin: str,
        name: str,
        program: _Program,
        stops: int,
    ) -> WorkerServiceState:
        """Start the service unless an instance of this program exists, crashed or not, then report it.

        ``stops`` is the service's stop count as the request arrived; a stop that arrived since refuses the start.
        """
        key = (plugin, name)
        async with self._lock:
            if self._stops.get(key, 0) != stops:
                raise HTTPException(409, "The service was stopped while it started.")
            instance = self._instances.get(key)
            if instance is not None and instance.entry_hash != program.entry_hash:
                await self._end(key, instance)
                instance = None
            if instance is None:
                instance = self._instances[key] = await _start(program)
                instance.expiry = asyncio.create_task(self._expire(key, instance), name="worker-service-idle")
            self.touch(plugin, name)
        return await _settled(instance)

    async def stop(self, plugin: str, name: str) -> None:
        """End the service's instance, if any, and refuse the starts that arrived before this stop."""
        key = (plugin, name)
        self._stops[key] = self._stops.get(key, 0) + 1
        async with self._lock:
            if (instance := self._instances.get(key)) is not None:
                await self._end(key, instance)

    async def close(self) -> None:
        """End every instance as the runner stops."""
        async with self._lock:
            for key, instance in list(self._instances.items()):
                await self._end(key, instance)

    async def _end(self, key: tuple[str, str], instance: _ServiceInstance) -> None:
        del self._instances[key]
        if instance.expiry is not None:
            instance.expiry.cancel()
        await _terminate(instance.process)

    async def _expire(self, key: tuple[str, str], instance: _ServiceInstance) -> None:
        loop = asyncio.get_running_loop()
        while True:
            # Each touch moves the deadline, so it is read again after every sleep, and under the lock an ensure holds.
            while (remaining := instance.idle_until - loop.time()) > 0:  # noqa: ASYNC110
                await asyncio.sleep(remaining)
            async with self._lock:
                if self._instances.get(key) is not instance:
                    return
                if instance.idle_until <= loop.time():
                    instance.expiry = None
                    await self._end(key, instance)
                    return


def worker_services(app: FastAPI) -> _WorkerServices:
    """Return this runner's service instances, created on first use."""
    try:
        return app.state.worker_services
    except AttributeError:
        services = app.state.worker_services = _WorkerServices()
        return services


@dataclass(frozen=True)
class RunningService:
    """A service running in this worker: its loopback port, the secret it requires, and its idle timeout."""

    port: int
    secret: str = field(repr=False)
    idle_timeout_seconds: float


def running_service(app: FastAPI, plugin: str, name: str) -> RunningService | None:
    """Return the plugin's service while its program runs, resetting its idle timer as an ensure does; else None."""
    services = worker_services(app)
    instance = services.instance(plugin, name)
    if instance is None or instance.process.returncode is not None:
        return None
    services.touch(plugin, name)
    return RunningService(instance.port, instance.secret, instance.idle_timeout_seconds)


async def close_worker_services(app: FastAPI) -> None:
    """End this runner's services as it stops."""
    await worker_services(app).close()


async def _start(program: _Program) -> _ServiceInstance:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    secret = secrets.token_urlsafe(32)
    # 🔒 A pipe's reader end can be opened again through /proc/<pid>/fd/0 by any process of the same user; a socket's not.
    ours, theirs = socket.socketpair()
    with ours, theirs:
        process = await asyncio.create_subprocess_exec(
            program.python,
            "-s",
            str(program.path),
            stdin=theirs.fileno(),
            env={**program.env, "MINDROOM_WORKER_SERVICE_PORT": str(port)},
            cwd=program.cwd,
            start_new_session=True,
        )
        # The secret fits the socket's buffer, so this never blocks; a program that already exited is reported as such.
        with suppress(OSError):
            ours.sendall(f"{secret}\n".encode())
    return _ServiceInstance(process, port, secret, program.entry_hash, program.idle_timeout_seconds)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Ask the instance's process group to end, then kill what is left of it after ``_STOP_SECONDS``."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STOP_SECONDS
    # The group outlives its leader while a process the leader started runs.
    while _group_alive(process.pid) and loop.time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def _settled(instance: _ServiceInstance) -> WorkerServiceState:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _START_SECONDS
    while instance.process.returncode is None:
        if await loopback_host(instance.port) is not None:
            return WorkerServiceState("running", instance.port)
        if loop.time() >= deadline:
            return WorkerServiceState("starting", instance.port)
        await asyncio.sleep(0.1)
    return WorkerServiceState("exited", instance.port, instance.process.returncode)


def _require_dedicated_worker(app: FastAPI) -> None:
    if not runner_uses_dedicated_worker(app_runtime_paths(app)):
        raise HTTPException(403, "Only a dedicated worker's runner runs plugin services.")


def _program(app: FastAPI, payload: _Ensure, plugin: str, name: str, runner_url: str) -> _Program:
    """Return the service's program in the plugin's folder, where the request's config places it here. Blocking."""
    route = prepare_route(app, payload)
    if isinstance(route, WorkerFailure):
        raise HTTPException(400, route.error)
    loaded = next(
        (p for p in refresh_plugins(route.config, route.runtime_paths).loaded_plugins if p.name == plugin),
        None,
    )
    if loaded is None:
        detail = f"Plugin {plugin!r} is not loaded in this worker: its folder is missing here, or it failed to load."
        raise HTTPException(404, detail)
    root = loaded.root.resolve()
    path = (root / payload.module).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(400, f"The service program {payload.module!r} is no file in plugin {plugin!r}.")
    # A dedicated worker's runner prepares every route's worker, which tool subprocesses run in too.
    assert route.worker is not None
    paths = route.worker.paths
    env = {
        **worker_subprocess_env(paths),
        "MINDROOM_WORKER_SERVICE_RUNNER_URL": runner_url,
        # The Computer serves the bridge (api/worker_computer.py) without the runner token this router requires.
        "MINDROOM_AGENT_BROWSER_URL": f"ws{runner_url.removeprefix('http')}/computer/agent-browser/{plugin}/{name}",
    }
    entry_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    python, workspace = str(paths.venv_dir / "bin" / "python"), str(paths.workspace)
    return _Program(path, entry_hash, python, env, workspace, payload.idle_timeout_seconds)


@router.post("/{plugin}/{name}")
async def ensure_service(request: Request, plugin: str, name: _Name, payload: _Ensure) -> WorkerServiceState:
    """Start the plugin's service unless it runs, and report it."""
    _require_dedicated_worker(request.app)
    runner_url = f"http://127.0.0.1:{request.scope['server'][1]}"
    services = worker_services(request.app)
    stops = services.stops(plugin, name)
    program = await asyncio.to_thread(_program, request.app, payload, plugin, name, runner_url)
    return await services.ensure(plugin, name, program, stops)


@router.delete("/{plugin}/{name}", status_code=204)
async def stop_service(request: Request, plugin: str, name: _Name) -> None:
    """End the plugin's service, if it runs."""
    _require_dedicated_worker(request.app)
    await worker_services(request.app).stop(plugin, name)
