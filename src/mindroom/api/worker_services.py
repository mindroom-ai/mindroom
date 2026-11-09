"""Runner routes that start, report, and stop the services plugins run in this dedicated worker.

A service is a program in the plugin's folder, which the request's config places as it does for ``/execute``, started as
a tool's subprocess is: with the worker's Python and sanitized environment, in a process group of its own. It serves on
the loopback port in ``MINDROOM_WORKER_SERVICE_PORT``, and ``MINDROOM_WORKER_SERVICE_RUNNER_URL`` is this runner's URL.
Every process in the worker can reach that port, so each start gets a fresh secret on stdin, which other processes
cannot read as they read the environment and command line; the program refuses requests whose
``X-MindRoom-Service-Secret`` header lacks it, and the secret never leaves this runner.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import signal
import socket
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Request
from pydantic import Field

from mindroom.api.sandbox_exec import resolve_subprocess_worker_context, runner_uses_dedicated_worker
from mindroom.api.sandbox_runner import app_runtime_paths, prepare_route, validate_runner_token
from mindroom.api.worker_preview import loopback_host
from mindroom.plugin_worker_services import SERVICE_NAME_PATTERN, WorkerServiceState
from mindroom.tool_system.plugins import refresh_plugins
from mindroom.workers.protocol import WorkerFailure, WorkerRoute

if TYPE_CHECKING:
    from pathlib import Path as FilePath

router = APIRouter(prefix="/api/sandbox-runner/plugin-services", tags=["sandbox-runner"])
_GUARDED = [Depends(validate_runner_token)]
_Name = Annotated[str, Path(pattern=f"^{SERVICE_NAME_PATTERN}$")]
# How long an ensure waits for a new instance to listen, and a stopped one's process group gets before it is killed.
_START_SECONDS = 10.0
_STOP_SECONDS = 5.0


class _Ensure(WorkerRoute):
    module: str = Field(min_length=1, max_length=4096)
    idle_timeout_seconds: float = Field(gt=0)


@dataclass(frozen=True)
class _Program:
    path: FilePath
    entry_hash: str
    python: str
    env: dict[str, str] = field(repr=False)
    cwd: str | None


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

    def instance(self, plugin: str, name: str) -> _ServiceInstance | None:
        """Return the service's instance, running or exited, or None."""
        return self._instances.get((plugin, name))

    def touch(self, plugin: str, name: str) -> None:
        """Reset the instance's idle timer, as traffic to it does."""
        if (instance := self.instance(plugin, name)) is not None:
            instance.idle_until = asyncio.get_running_loop().time() + instance.idle_timeout_seconds

    async def ensure(
        self,
        plugin: str,
        name: str,
        program: _Program,
        idle_timeout_seconds: float,
    ) -> WorkerServiceState:
        """Start the service unless an instance of this program exists, crashed or not, then report it."""
        key = (plugin, name)
        async with self._lock:
            instance = self._instances.get(key)
            if instance is not None and instance.entry_hash != program.entry_hash:
                await self._end(key, instance)
                instance = None
            if instance is None:
                instance = self._instances[key] = await _start(program, idle_timeout_seconds)
                instance.expiry = asyncio.create_task(self._expire(key, instance), name="worker-service-idle")
            self.touch(plugin, name)
        return await _settled(instance)

    async def stop(self, plugin: str, name: str) -> None:
        """End the service's instance, if any."""
        async with self._lock:
            if (instance := self._instances.get((plugin, name))) is not None:
                await self._end((plugin, name), instance)

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
        # Each touch moves the deadline, so the loop reads it again after every sleep.
        while (remaining := instance.idle_until - loop.time()) > 0:  # noqa: ASYNC110
            await asyncio.sleep(remaining)
        async with self._lock:
            if self._instances.get(key) is instance:
                instance.expiry = None
                await self._end(key, instance)


def _services(app: FastAPI) -> _WorkerServices:
    """Return this runner's service instances, created on first use."""
    try:
        return app.state.worker_services
    except AttributeError:
        services = app.state.worker_services = _WorkerServices()
        return services


async def close_worker_services(app: FastAPI) -> None:
    """End this runner's services as it stops."""
    await _services(app).close()


async def _start(program: _Program, idle_timeout_seconds: float) -> _ServiceInstance:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    secret = secrets.token_urlsafe(32)
    process = await asyncio.create_subprocess_exec(
        *(program.python, "-s", str(program.path)),
        stdin=asyncio.subprocess.PIPE,
        env={**program.env, "MINDROOM_WORKER_SERVICE_PORT": str(port)},
        cwd=program.cwd,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(f"{secret}\n".encode())
    # A program that exits before reading its secret is reported as exited.
    with suppress(ConnectionError):
        await process.stdin.drain()
    process.stdin.close()
    return _ServiceInstance(process, port, secret, program.entry_hash, idle_timeout_seconds)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """End the instance's process group, killing what is left of it after ``_STOP_SECONDS``."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), _STOP_SECONDS)
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def _settled(instance: _ServiceInstance) -> WorkerServiceState:
    deadline = asyncio.get_running_loop().time() + _START_SECONDS
    while instance.process.returncode is None:
        if await loopback_host(instance.port) is not None:
            return WorkerServiceState("running", instance.port)
        if asyncio.get_running_loop().time() >= deadline:
            return WorkerServiceState("starting", instance.port)
        await asyncio.sleep(0.1)
    return WorkerServiceState("exited", instance.port, instance.process.returncode)


def _require_dedicated_worker(app: FastAPI) -> None:
    if not runner_uses_dedicated_worker(app_runtime_paths(app)):
        raise HTTPException(403, "Only a dedicated worker's runner runs plugin services.")


def _program(app: FastAPI, payload: _Ensure, plugin: str, runner_url: str) -> _Program:
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
    python, env, cwd = resolve_subprocess_worker_context(route.worker.paths if route.worker is not None else None)
    env = {**(env or {}), "MINDROOM_WORKER_SERVICE_RUNNER_URL": runner_url}
    return _Program(path, hashlib.sha256(path.read_bytes()).hexdigest(), python or sys.executable, env, cwd)


@router.post("/{plugin}/{name}", dependencies=_GUARDED)
async def ensure_service(request: Request, plugin: _Name, name: _Name, payload: _Ensure) -> WorkerServiceState:
    """Start the plugin's service unless it runs, and report it."""
    _require_dedicated_worker(request.app)
    runner_url = f"http://127.0.0.1:{request.scope['server'][1]}"
    program = await asyncio.to_thread(_program, request.app, payload, plugin, runner_url)
    return await _services(request.app).ensure(plugin, name, program, payload.idle_timeout_seconds)


@router.delete("/{plugin}/{name}", status_code=204, dependencies=_GUARDED)
async def stop_service(request: Request, plugin: _Name, name: _Name) -> None:
    """End the plugin's service, if it runs."""
    _require_dedicated_worker(request.app)
    await _services(request.app).stop(plugin, name)
