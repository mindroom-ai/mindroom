"""The primary's side of plugin worker services, which ``mindroom.api.worker_services`` runs in dedicated workers.

A service never runs in the primary: without a dedicated worker backend every call fails. Each plugin load replaces
every plugin's code, so the primary stops the services it started whenever it loads plugins again or stops.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple

import httpx
from pydantic import TypeAdapter, ValidationError

from mindroom.http_error_detail import error_detail_from_response
from mindroom.logging_config import get_logger
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config, worker_proxy_client_config, worker_route
from mindroom.tool_system.worker_proxy_client import worker_request_headers
from mindroom.workers.backend_kinds import worker_backend_kind
from mindroom.workers.models import worker_api_root
from mindroom.workers.runtime import lease_primary_worker_manager

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.plugin_context import PluginContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

SERVICE_NAME_PATTERN = "[a-z0-9][a-z0-9_-]{0,63}"


@dataclass(frozen=True)
class WorkerService:
    """A program in a plugin's folder, ``module``, that an agent's dedicated worker runs on demand."""

    name: str
    module: str
    idle_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        """Refuse a name the runner's routes cannot carry and a timeout that is not positive."""
        if not re.fullmatch(SERVICE_NAME_PATTERN, self.name):
            msg = f"Worker service name {self.name!r} must be 1-64 lowercase letters, digits, '-' or '_'."
            raise ValueError(msg)
        if self.idle_timeout_seconds <= 0:
            msg = "A worker service's idle_timeout_seconds must be positive."
            raise ValueError(msg)


@dataclass(frozen=True)
class WorkerServiceState:
    """A service as an ensure found it: ``running`` once its port accepts connections, ``exited`` once it ended."""

    state: Literal["starting", "running", "exited"]
    port: int
    exit_code: int | None = None


class WorkerServiceError(RuntimeError):
    """A worker service could not be reached: no dedicated worker backend, no worker, or the runner refused."""


class _Endpoint(NamedTuple):
    """The route of a target's worker, its runner's service routes, and the headers that authenticate to it."""

    route: dict[str, object]
    root: str
    headers: dict[str, str]


_STATE = TypeAdapter(WorkerServiceState)
# The services this primary started, by worker key, plugin, and name, with what reaches their worker again.
_started: dict[tuple[str, str, str], tuple[RuntimePaths, Config, ResolvedWorkerTarget]] = {}


def _endpoint(
    runtime_paths: RuntimePaths,
    config: Config,
    target: ResolvedWorkerTarget,
    *,
    start: bool,
) -> _Endpoint | None:
    """Return the target's worker's endpoint, None while it does not run. Blocking."""
    if not worker_backend_kind(runtime_paths).dedicated:
        msg = "Worker services need a dedicated worker backend: set MINDROOM_WORKER_BACKEND to docker or kubernetes."
        raise WorkerServiceError(msg)
    proxy_config = sandbox_proxy_config(runtime_paths)
    try:
        with lease_primary_worker_manager(
            runtime_paths,
            proxy_url=proxy_config.proxy_url,
            proxy_token=proxy_config.proxy_token,
        ) as manager:
            route, handle = worker_route(
                runtime_paths=runtime_paths,
                runtime_config=config,
                tool_name="worker service",
                function_name="ensure" if start else "stop",
                worker_target=target,
                worker_manager=manager,
                base_dir=None,
                start=start,
            )
        if handle is None or handle.status != "ready":
            return None
        headers = worker_request_headers(handle, config=worker_proxy_client_config(proxy_config, runtime_paths))
    except RuntimeError as error:
        msg = f"The agent's worker is unavailable: {error}"
        raise WorkerServiceError(msg) from None
    return _Endpoint(route.model_dump(mode="json"), f"{worker_api_root(handle)}/plugin-services", headers)


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
    endpoint = await asyncio.to_thread(_endpoint, paths, config, target, start=True)
    if endpoint is None:
        msg = "The agent's worker is not ready."
        raise WorkerServiceError(msg)
    _started[(str(endpoint.route["worker_key"]), plugin, service.name)] = (paths, config, target)
    body = {**endpoint.route, "module": service.module, "idle_timeout_seconds": service.idle_timeout_seconds}
    answer = await _send("POST", endpoint, f"{plugin}/{service.name}", body)
    try:
        return _STATE.validate_json(answer)
    except ValidationError:
        msg = "The agent's worker answered something other than a service state."
        raise WorkerServiceError(msg) from None


async def _stop(paths: RuntimePaths, config: Config, target: ResolvedWorkerTarget, plugin: str, name: str) -> None:
    endpoint = await asyncio.to_thread(_endpoint, paths, config, target, start=False)
    # A worker that does not run runs no service.
    if endpoint is not None:
        _started.pop((str(endpoint.route["worker_key"]), plugin, name), None)
        await _send("DELETE", endpoint, f"{plugin}/{name}", None)


async def stop_worker_service(context: PluginContext, service: WorkerService, target: ResolvedWorkerTarget) -> None:
    """Stop the plugin's service in the target's worker, if it runs; a stopped worker is not started."""
    await _stop(context.runtime_paths, context.config, target, context.plugin_name, service.name)


async def stop_started_worker_services() -> None:
    """Stop every service this primary started, as a plugin load or shutdown replaces the code that started them."""
    started = list(_started.items())
    _started.clear()
    stops = (_stop(paths, config, target, plugin, name) for (_, plugin, name), (paths, config, target) in started)
    for ((_, plugin, name), _), result in zip(
        started,
        await asyncio.gather(*stops, return_exceptions=True),
        strict=True,
    ):
        if isinstance(result, Exception):
            logger.warning("Failed to stop a worker service", plugin_name=plugin, service=name, error=str(result))
