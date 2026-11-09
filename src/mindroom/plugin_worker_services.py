"""The primary's side of plugin worker services, which ``mindroom.api.worker_services`` runs in dedicated workers.

A service never runs in the primary: without a dedicated worker backend every call fails. Each plugin load replaces
every plugin's code, so the primary stops the services it started whenever it loads plugins again or stops.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple

import httpx
from pydantic import TypeAdapter, ValidationError

from mindroom.http_error_detail import error_detail_from_response
from mindroom.logging_config import get_logger
from mindroom.tool_system.sandbox_proxy import (
    lease_worker_manager,
    sandbox_proxy_config,
    worker_proxy_client_config,
    worker_route,
)
from mindroom.tool_system.worker_proxy_client import worker_request_headers
from mindroom.workers.backend_kinds import worker_backend_kind
from mindroom.workers.models import worker_api_root

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.plugin_context import PluginContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.models import WorkerHandle
    from mindroom.workers.protocol import WorkerRoute

logger = get_logger(__name__)

SERVICE_NAME_PATTERN = "[a-z0-9][a-z0-9_-]{0,63}"


@dataclass(frozen=True)
class WorkerService:
    """A program in a plugin's folder, ``module``, that an agent's dedicated worker runs on demand."""

    name: str
    module: str
    idle_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        """Refuse a name the runner's routes cannot carry and a timeout that is not a positive number of seconds."""
        if not re.fullmatch(SERVICE_NAME_PATTERN, self.name):
            msg = f"Worker service name {self.name!r} must be 1-64 lowercase letters, digits, '-' or '_', starting with a letter or digit."
            raise ValueError(msg)
        if not math.isfinite(self.idle_timeout_seconds) or self.idle_timeout_seconds <= 0:
            msg = "A worker service's idle_timeout_seconds must be positive and finite."
            raise ValueError(msg)


@dataclass(frozen=True)
class WorkerServiceState:
    """A service as an ensure found it: ``running`` once its processes alone listen on its port, ``exited`` once it ended."""

    state: Literal["starting", "running", "exited"]
    port: int
    exit_code: int | None = None


class WorkerServiceError(RuntimeError):
    """A worker service could not be reached: no dedicated worker backend, no worker, or the runner refused."""


class _Endpoint(NamedTuple):
    """A worker runner's service routes and the headers that authenticate to it."""

    root: str
    headers: dict[str, str]


_STATE = TypeAdapter(WorkerServiceState)
# The services this primary started, by worker key, plugin, and name, with the runtime that started them.
_started: dict[tuple[str, str, str], RuntimePaths] = {}
# The ensures under way, which a plugin load waits for, so that it also stops what they start.
_pending: set[asyncio.Future[None]] = set()


def _reachable(handle: WorkerHandle | None, paths: RuntimePaths) -> _Endpoint | None:
    if handle is None or handle.status != "ready":
        return None
    headers = worker_request_headers(handle, config=worker_proxy_client_config(sandbox_proxy_config(paths), paths))
    return _Endpoint(f"{worker_api_root(handle)}/plugin-services", headers)


def _target_worker(
    paths: RuntimePaths,
    config: Config,
    target: ResolvedWorkerTarget,
    *,
    start: bool,
) -> tuple[WorkerRoute, _Endpoint | None]:
    """Return the target's worker's route, and its endpoint unless it does not run. Blocking."""
    if not worker_backend_kind(paths).dedicated:
        msg = "Worker services need a dedicated worker backend: set MINDROOM_WORKER_BACKEND to docker or kubernetes."
        raise WorkerServiceError(msg)
    proxy_config = sandbox_proxy_config(paths)
    try:
        with lease_worker_manager(paths, proxy_config) as manager:
            route, handle = worker_route(
                runtime_paths=paths,
                runtime_config=config,
                tool_name="worker service",
                function_name="ensure" if start else "stop",
                worker_target=target,
                worker_manager=manager,
                base_dir=None,
                start=start,
            )
        return route, _reachable(handle, paths)
    except RuntimeError as error:
        msg = f"The agent's worker is unavailable: {error}"
        raise WorkerServiceError(msg) from None


def _recorded_worker(paths: RuntimePaths, worker_key: str) -> _Endpoint | None:
    """Return the endpoint of the worker a service started in, unless it no longer runs. Blocking."""
    proxy_config = sandbox_proxy_config(paths)
    try:
        with lease_worker_manager(paths, proxy_config) as manager:
            return _reachable(manager.touch_worker(worker_key), paths)
    except RuntimeError as error:
        msg = f"The agent's worker is unavailable: {error}"
        raise WorkerServiceError(msg) from None


async def _send(method: str, endpoint: _Endpoint, path: str, body: object) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(method, f"{endpoint.root}/{path}", headers=endpoint.headers, json=body)
    except httpx.HTTPError as error:
        msg = f"The agent's worker is unavailable: {type(error).__name__}"
        raise WorkerServiceError(msg) from None
    if response.status_code >= 400:
        detail = error_detail_from_response(response, fallback=f"HTTP {response.status_code}")
        msg = f"The agent's worker refused: {detail}"
        raise WorkerServiceError(msg)
    return response.content


async def ensure_worker_service(
    context: PluginContext,
    service: WorkerService,
    target: ResolvedWorkerTarget,
) -> WorkerServiceState:
    """Start the plugin's service in the target's worker unless it runs there, starting the worker as a tool call does."""
    paths, config, plugin = context.runtime_paths, context.config, context.plugin_name
    done = asyncio.get_running_loop().create_future()
    _pending.add(done)
    try:
        route, endpoint = await asyncio.to_thread(_target_worker, paths, config, target, start=True)
        if endpoint is None:
            msg = "The agent's worker is not ready."
            raise WorkerServiceError(msg)
        # A dedicated backend's route names the worker the service starts in, a subagent's own worker included.
        assert route.worker_key is not None
        _started[(route.worker_key, plugin, service.name)] = paths
        body = {
            **route.model_dump(mode="json"),
            "module": service.module,
            "idle_timeout_seconds": service.idle_timeout_seconds,
        }
        answer = await _send("POST", endpoint, f"{plugin}/{service.name}", body)
    finally:
        _pending.discard(done)
        done.set_result(None)
    try:
        return _STATE.validate_json(answer)
    except ValidationError:
        msg = "The agent's worker answered something other than a service state."
        raise WorkerServiceError(msg) from None


async def _stop(endpoint: _Endpoint, worker_key: str, plugin: str, name: str) -> None:
    # Forgotten before the stop, so an ensure meanwhile records the service anew; kept again if the stop fails.
    key = (worker_key, plugin, name)
    paths = _started.pop(key, None)
    try:
        await _send("DELETE", endpoint, f"{plugin}/{name}", None)
    except WorkerServiceError:
        if paths is not None:
            _started.setdefault(key, paths)
        raise


async def stop_worker_service(context: PluginContext, service: WorkerService, target: ResolvedWorkerTarget) -> None:
    """Stop the plugin's service in the target's worker, if it runs; a stopped worker is not started."""
    paths, plugin = context.runtime_paths, context.plugin_name
    route, endpoint = await asyncio.to_thread(_target_worker, paths, context.config, target, start=False)
    # A worker that does not run runs no service.
    if endpoint is not None:
        assert route.worker_key is not None
        await _stop(endpoint, route.worker_key, plugin, service.name)


async def _stop_recorded(paths: RuntimePaths, worker_key: str, plugin: str, name: str) -> None:
    if (endpoint := await asyncio.to_thread(_recorded_worker, paths, worker_key)) is not None:
        await _stop(endpoint, worker_key, plugin, name)


async def stop_started_worker_services() -> None:
    """Stop every service this primary started, as a plugin load or shutdown replaces the code that started them.

    Ensures under way finish first, so the services they start stop too; each stops in the worker it started in.
    """
    if _pending:
        await asyncio.wait(set(_pending))
    started = list(_started.items())
    _started.clear()
    results = await asyncio.gather(
        *(_stop_recorded(paths, *key) for key, paths in started),
        return_exceptions=True,
    )
    for (key, paths), result in zip(started, results, strict=True):
        if isinstance(result, Exception):
            # The next load or shutdown tries again, unless an ensure since recorded the service anew.
            _started.setdefault(key, paths)
            _, plugin, name = key
            logger.warning("Failed to stop a worker service", plugin_name=plugin, service=name, error=str(result))
