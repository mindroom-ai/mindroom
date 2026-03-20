"""Sandbox runner API for executing tool calls inside isolated containers."""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import secrets
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from mindroom import constants
from mindroom.api import sandbox_exec, sandbox_protocol, sandbox_worker_prep
from mindroom.config.main import Config, _normalized_config_data, load_config
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.tool_system.catalog import (
    SAFE_TOOL_INIT_OVERRIDE_FIELDS,
    TOOL_METADATA,
    ToolConfigOverrideError,
    ToolInitOverrideError,
    ToolValidationInfo,
    deserialize_tool_validation_snapshot,
    ensure_tool_registry_loaded,
    get_tool_by_name,
    sanitize_tool_init_overrides,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.sandbox_proxy import (
    sandbox_proxy_config,
    to_json_compatible,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    build_worker_target_from_runtime_env,
    tool_execution_identity,
)
from mindroom.workers.backends.local import get_local_worker_manager

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools.toolkit import Toolkit

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.catalog import ToolValidationInfo
    from mindroom.workers.models import WorkerHandle

logger = get_logger(__name__)

_SUBPROCESS_WORKER_ARG = "--sandbox-subprocess-worker"
_RUNNER_TOKEN_ENV = "MINDROOM_SANDBOX_PROXY_TOKEN"  # noqa: S105


def _startup_manifest_path_from_env() -> Path:
    raw_path = os.environ.get(constants.SANDBOX_STARTUP_MANIFEST_PATH_ENV, "").strip()
    if not raw_path:
        msg = f"{constants.SANDBOX_STARTUP_MANIFEST_PATH_ENV} must be set for sandbox runner startup."
        raise RuntimeError(msg)
    return Path(raw_path).expanduser()


def _startup_manifest_from_env() -> dict[str, object]:
    payload = json.loads(_startup_manifest_path_from_env().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"{constants.SANDBOX_STARTUP_MANIFEST_PATH_ENV} must point to a JSON object."
        raise TypeError(msg)
    return payload


def _startup_runtime_paths_from_env() -> RuntimePaths:
    """Read the committed sandbox-runner runtime payload from the startup manifest."""
    startup_runtime_paths, _tool_validation_snapshot = constants.deserialize_startup_manifest(
        _startup_manifest_from_env(),
    )
    if sandbox_exec.runner_uses_dedicated_worker(startup_runtime_paths):
        return startup_runtime_paths
    process_env = dict(startup_runtime_paths.process_env)
    process_env.update(
        {
            key: value
            for key, value in os.environ.items()
            if key not in {_RUNNER_TOKEN_ENV, constants.SANDBOX_STARTUP_MANIFEST_PATH_ENV}
        },
    )
    config_path = (
        Path(process_env["MINDROOM_CONFIG_PATH"])
        if process_env.get("MINDROOM_CONFIG_PATH")
        else startup_runtime_paths.config_path
    )
    storage_path = (
        Path(process_env["MINDROOM_STORAGE_PATH"])
        if process_env.get("MINDROOM_STORAGE_PATH")
        else startup_runtime_paths.storage_root
    )
    config_path = (
        Path(process_env["MINDROOM_CONFIG_PATH"])
        if process_env.get("MINDROOM_CONFIG_PATH")
        else startup_runtime_paths.config_path
    )
    storage_path = (
        Path(process_env["MINDROOM_STORAGE_PATH"])
        if process_env.get("MINDROOM_STORAGE_PATH")
        else startup_runtime_paths.storage_root
    )
    resolved_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=storage_path,
        process_env=process_env,
    )
    env_file_values = dict(startup_runtime_paths.env_file_values)
    env_file_values.update(resolved_runtime_paths.env_file_values)
    return constants.RuntimePaths(
        config_path=resolved_runtime_paths.config_path,
        config_dir=resolved_runtime_paths.config_dir,
        env_path=resolved_runtime_paths.env_path,
        storage_root=resolved_runtime_paths.storage_root,
        process_env=resolved_runtime_paths.process_env,
        env_file_values=MappingProxyType(env_file_values),
    )


def _startup_runner_token_from_env() -> str | None:
    raw_token = os.environ.get(_RUNNER_TOKEN_ENV, "").strip()
    return raw_token or None


def _upstream_tool_validation_snapshot(runtime_paths: RuntimePaths) -> dict[str, ToolValidationInfo]:
    startup_manifest_path = constants.sandbox_startup_manifest_path(runtime_paths.storage_root)
    if not startup_manifest_path.exists():
        return {}
    startup_runtime_paths, tool_validation_snapshot = constants.deserialize_startup_manifest(
        json.loads(startup_manifest_path.read_text(encoding="utf-8")),
    )
    if startup_runtime_paths.storage_root != runtime_paths.storage_root:
        msg = "Sandbox startup manifest storage_root does not match runtime storage_root."
        raise RuntimeError(msg)
    return deserialize_tool_validation_snapshot(tool_validation_snapshot)


def _runtime_config_or_empty(runtime_paths: RuntimePaths) -> Config:
    """Return the runtime config visible inside one sandbox runner."""
    if runtime_paths.config_path.exists():
        if not sandbox_exec.runner_uses_dedicated_worker(runtime_paths):
            return load_config(runtime_paths)
        return _dedicated_worker_runtime_config_or_empty(runtime_paths)
    return Config.validate_with_runtime({}, runtime_paths)


def _dedicated_worker_runtime_config_or_empty(runtime_paths: RuntimePaths) -> Config:
    """Return dedicated-worker config, tolerating plugins unavailable in that worker image."""
    with runtime_paths.config_path.open() as f:
        data = yaml.safe_load(f) or {}

    tool_validation_snapshot = _upstream_tool_validation_snapshot(runtime_paths)
    if not tool_validation_snapshot:
        return load_config(runtime_paths)

    # Dedicated workers only need the authored config shape plus the subset of
    # plugin entries that actually exist in that runtime filesystem. The primary
    # runtime is authoritative for full authored tool validation; workers
    # validate the requested tool at execution time with their local registry.
    config = Config.model_validate(
        _normalized_config_data(data),
        context={"runtime_paths": runtime_paths},
    )
    return _config_with_available_plugins(config, runtime_paths)


def _config_with_available_plugins(config: Config, runtime_paths: RuntimePaths) -> Config:
    """Return one config snapshot filtered to plugin entries visible in this runtime."""
    if not config.plugins:
        return config

    from mindroom.tool_system import plugin_imports  # noqa: PLC0415

    available_plugins = []
    skipped_plugin_paths: list[str] = []
    for plugin_entry in config.plugins:
        if not plugin_entry.enabled:
            available_plugins.append(plugin_entry)
            continue

        try:
            plugin_root = plugin_imports._resolve_plugin_root(plugin_entry.path, runtime_paths)
        except Exception:
            skipped_plugin_paths.append(plugin_entry.path)
            continue

        if plugin_root.exists() and plugin_root.is_dir():
            available_plugins.append(plugin_entry)
        else:
            skipped_plugin_paths.append(plugin_entry.path)

    if not skipped_plugin_paths:
        return config

    logger.info(
        "sandbox_runner_skipping_unavailable_plugins",
        plugin_paths=sorted(skipped_plugin_paths),
    )
    return config.model_copy(update={"plugins": available_plugins}, deep=True)


def _load_config_from_startup_runtime() -> tuple[RuntimePaths, Config]:
    """Read the sandbox runner runtime context from explicit startup payload."""
    runtime_paths = _startup_runtime_paths_from_env()
    return runtime_paths, _runtime_config_or_empty(runtime_paths)


def initialize_sandbox_runner_app(
    api_app: FastAPI,
    runtime_paths: RuntimePaths,
    *,
    config: Config | None = None,
    runner_token: str | None = None,
) -> None:
    """Attach one explicit runtime context to a sandbox-runner app instance."""
    committed_config = config or _runtime_config_or_empty(runtime_paths)
    ensure_registry_loaded_with_config(runtime_paths, committed_config)
    api_app.state.sandbox_runner_context = _SandboxRunnerContext(
        runtime_paths=runtime_paths,
        config=committed_config,
        tool_metadata=TOOL_METADATA.copy(),
        runner_token=runner_token or sandbox_proxy_config(runtime_paths).proxy_token,
    )


def ensure_registry_loaded_with_config(runtime_paths: RuntimePaths, config: Config) -> None:
    """Load config from env and ensure the tool registry is populated.

    Used by both the FastAPI startup and the subprocess worker so that
    plugin tools are registered even in fresh processes.
    """
    ensure_tool_registry_loaded(runtime_paths, config)


def _runner_credentials_manager(runtime_paths: RuntimePaths) -> CredentialsManager:
    """Return the sandbox runner's persisted credential manager."""
    return get_runtime_credentials_manager(runtime_paths)


def _request_private_agent_names(request: SandboxRunnerExecuteRequest) -> frozenset[str] | None:
    """Return the explicit user-agent visibility snapshot carried by one request."""
    return _freeze_private_agent_names(request.private_agent_names)


def _freeze_private_agent_names(private_agent_names: list[str] | None) -> frozenset[str] | None:
    """Freeze one optional private-agent visibility snapshot."""
    if private_agent_names is None:
        return None
    return frozenset(private_agent_names)


def _filter_runtime_tool_init_overrides(tool_name: str, runtime_overrides: dict[str, object]) -> dict[str, object]:
    """Keep only runtime init overrides declared by the target tool."""
    metadata = TOOL_METADATA.get(tool_name)
    if metadata is None or not metadata.config_fields:
        return {}
    allowed_field_names = {field.name for field in metadata.config_fields}
    supported_runtime_overrides = {
        name: value
        for name, value in runtime_overrides.items()
        if name in allowed_field_names and name in SAFE_TOOL_INIT_OVERRIDE_FIELDS
    }
    return sanitize_tool_init_overrides(tool_name, supported_runtime_overrides) or {}


def _request_runtime_overrides(
    request: SandboxRunnerExecuteRequest,
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None,
) -> dict[str, object] | None:
    """Return runtime overrides for one runner-side tool rebuild."""
    runtime_overrides = sandbox_worker_prep.ready_runtime_overrides(
        prepared_worker.runtime_overrides if prepared_worker is not None else None,
    )
    if request.tool_name != "shell" or request.extra_env_passthrough is None:
        return runtime_overrides

    # Pre-resolve passthrough patterns against only the client's env snapshot
    # to prevent cross-runtime secret leakage via glob patterns that match
    # runner-only env vars.
    resolved = constants.shell_extra_env_values(
        extra_env_passthrough=request.extra_env_passthrough,
        process_env=request.execution_env,
    )
    if not resolved:
        return runtime_overrides

    merged_runtime_overrides = dict(runtime_overrides or {})
    merged_runtime_overrides["extra_env_passthrough"] = ",".join(resolved.keys())
    return merged_runtime_overrides


class SandboxRunnerExecuteRequest(BaseModel):
    """Tool call payload forwarded from a primary runtime to the sandbox runtime.

    Clients must provide credentials via ``lease_id``.
    ``credential_overrides`` is reserved for internal in-process and subprocess
    execution after the lease has been resolved.
    ``execution_env`` is reserved for execution tools such as ``shell`` and
    sandboxed ``python`` that intentionally receive runtime env during execution.
    """

    tool_name: str
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    lease_id: str | None = None
    worker_key: str | None = None
    worker_scope: WorkerScope | None = None
    routing_agent_name: str | None = None
    execution_identity: dict[str, Any] = Field(default_factory=dict)
    private_agent_names: list[str] | None = None
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_config_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_init_overrides: dict[str, Any] = Field(default_factory=dict)
    execution_env: dict[str, str] = Field(default_factory=dict)
    extra_env_passthrough: str | None = None


class PreparedSandboxRunnerExecuteRequest(BaseModel):
    """Prepared sandbox request shared by in-process and subprocess execution."""

    tool_name: str
    function_name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    worker_key: str | None = None
    worker_scope: WorkerScope | None = None
    routing_agent_name: str | None = None
    execution_identity: dict[str, Any] = Field(default_factory=dict)
    private_agent_names: list[str] | None = None
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_config_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_init_overrides: dict[str, Any] = Field(default_factory=dict)
    runtime_overrides: dict[str, Any] = Field(default_factory=dict)


class SandboxRunnerLeaseRequest(BaseModel):
    """Request for creating a short-lived credential lease."""

    tool_name: str
    function_name: str
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = sandbox_worker_prep.DEFAULT_LEASE_TTL_SECONDS
    max_uses: int = 1


class SandboxRunnerLeaseResponse(BaseModel):
    """Response describing a created credential lease."""

    lease_id: str
    expires_at: float
    max_uses: int


class SandboxRunnerExecuteResponse(BaseModel):
    """Sandbox tool execution response."""

    ok: bool
    result: Any | None = None
    error: str | None = None
    failure_kind: Literal["tool", "worker"] | None = None


class SandboxWorkerResponse(BaseModel):
    """Serialized worker metadata for sandbox-runner observability."""

    worker_id: str
    worker_key: str
    endpoint: str
    status: str
    backend_name: str
    last_used_at: float
    created_at: float
    last_started_at: float | None = None
    expires_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None
    debug_metadata: dict[str, str] = Field(default_factory=dict)


class SandboxWorkerListResponse(BaseModel):
    """List of known sandbox workers."""

    workers: list[SandboxWorkerResponse]


class SandboxWorkerCleanupResponse(BaseModel):
    """Result of one idle-worker cleanup pass."""

    idle_timeout_seconds: float
    cleaned_workers: list[SandboxWorkerResponse]


@dataclass(frozen=True)
class _SandboxRunnerContext:
    runtime_paths: RuntimePaths
    config: Config
    tool_metadata: dict[str, Any]
    runner_token: str | None


@dataclass(frozen=True)
class _PreparedSandboxRequestContext:
    request: PreparedSandboxRunnerExecuteRequest
    runtime_paths: RuntimePaths
    execution_env: dict[str, str]
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None


@dataclass(frozen=True)
class _PreparedSandboxSubprocessContext:
    python_executable: str | None
    subprocess_env: dict[str, str] | None
    subprocess_cwd: str | None


def _app_context(app: FastAPI) -> _SandboxRunnerContext:
    try:
        context = app.state.sandbox_runner_context
    except AttributeError:
        context = None
    if not isinstance(context, _SandboxRunnerContext):
        msg = "Sandbox runner context is not initialized"
        raise TypeError(msg)
    return context


def _app_runtime_paths(app: FastAPI) -> RuntimePaths:
    return _app_context(app).runtime_paths


def _app_runtime_config(app: FastAPI) -> Config:
    return _app_context(app).config


def _app_tool_metadata(app: FastAPI) -> dict[str, Any]:
    return _app_context(app).tool_metadata


def _app_runner_token(app: FastAPI) -> str | None:
    runner_token = _app_context(app).runner_token
    if runner_token is None:
        return None
    if not isinstance(runner_token, str):
        msg = "Sandbox runner token is not initialized"
        raise TypeError(msg)
    return runner_token


def sandbox_runner_runtime_paths(request: Request) -> RuntimePaths:
    """Return the committed runtime paths for one sandbox runner request."""
    return _app_runtime_paths(request.app)


def sandbox_runner_runtime_config(request: Request) -> Config:
    """Return the committed validated config for one sandbox runner request."""
    return _app_runtime_config(request.app)


def sandbox_runner_tool_metadata(request: Request) -> dict[str, Any]:
    """Return the committed tool metadata snapshot for one sandbox runner request."""
    return _app_tool_metadata(request.app)


async def _validate_runner_token(
    request: Request,
    x_mindroom_sandbox_token: Annotated[str | None, Header()] = None,
) -> None:
    proxy_token = _app_runner_token(request.app)
    if proxy_token is None:
        raise HTTPException(status_code=503, detail="Sandbox runner token is not configured.")
    if not secrets.compare_digest(x_mindroom_sandbox_token or "", proxy_token):
        raise HTTPException(status_code=401, detail="Unauthorized sandbox runner request")


router = APIRouter(
    prefix="/api/sandbox-runner",
    tags=["sandbox-runner"],
    dependencies=[Depends(_validate_runner_token)],
)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _resolve_entrypoint(
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    tool_name: str,
    function_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
    credential_overrides: dict[str, object] | None = None,
    tool_config_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
    private_agent_names: frozenset[str] | None = None,
) -> tuple[Toolkit, Callable[..., object]]:
    ensure_registry_loaded_with_config(runtime_paths, config)
    worker_target = build_worker_target_from_runtime_env(
        worker_scope,
        routing_agent_name,
        execution_identity=execution_identity,
        runtime_paths=runtime_paths,
        private_agent_names=private_agent_names,
    )
    try:
        toolkit = get_tool_by_name(
            tool_name,
            runtime_paths=runtime_paths,
            disable_sandbox_proxy=True,
            credential_overrides=credential_overrides,
            credentials_manager=_runner_credentials_manager(runtime_paths),
            tool_config_overrides=tool_config_overrides,
            tool_init_overrides=tool_init_overrides,
            runtime_overrides=runtime_overrides,
            allowed_shared_services=(config.get_worker_grantable_credentials() if worker_scope is not None else None),
            worker_target=worker_target,
        )
    except (ToolConfigOverrideError, ToolInitOverrideError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    function = toolkit.functions.get(function_name) or toolkit.async_functions.get(function_name)
    if function is None or function.entrypoint is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' does not expose '{function_name}'.")
    return toolkit, function.entrypoint


def _serialize_worker(worker: WorkerHandle) -> SandboxWorkerResponse:
    return SandboxWorkerResponse(
        worker_id=worker.worker_id,
        worker_key=worker.worker_key,
        endpoint=worker.endpoint,
        status=worker.status,
        backend_name=worker.backend_name,
        last_used_at=worker.last_used_at,
        created_at=worker.created_at,
        last_started_at=worker.last_started_at,
        expires_at=worker.expires_at,
        startup_count=worker.startup_count,
        failure_count=worker.failure_count,
        failure_reason=worker.failure_reason,
        debug_metadata=worker.debug_metadata,
    )


def _prepared_tool_init_overrides(
    tool_name: str,
    tool_init_overrides: dict[str, object],
    runtime_overrides: dict[str, object] | None,
) -> dict[str, object]:
    """Merge explicit and runtime-derived init overrides for one prepared request."""
    prepared_tool_init_overrides = dict(tool_init_overrides)
    serialized_runtime_overrides = to_json_compatible(runtime_overrides)
    if not isinstance(serialized_runtime_overrides, dict):
        return prepared_tool_init_overrides

    runtime_override_payload: dict[str, object] = {
        name: value for name, value in serialized_runtime_overrides.items() if isinstance(name, str)
    }
    prepared_tool_init_overrides.update(
        _filter_runtime_tool_init_overrides(tool_name, runtime_override_payload),
    )
    return prepared_tool_init_overrides


def _prepare_execute_request(
    request: SandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None = None,
    *,
    runner_token: str | None = None,
) -> _PreparedSandboxRequestContext:
    execution_env = sandbox_exec.request_execution_env(request.tool_name, request.execution_env, runtime_paths)
    private_agent_names = _request_private_agent_names(request)
    prepared = sandbox_worker_prep.resolve_prepared_worker_request(
        worker_key=request.worker_key,
        tool_init_overrides=request.tool_init_overrides,
        runtime_paths=runtime_paths,
        private_agent_names=private_agent_names,
        prepared_worker=prepared_worker,
        runner_token=runner_token,
    )
    if request.tool_name == "shell" and prepared is not None:
        worker_execution_env = sandbox_exec.worker_subprocess_env(prepared.paths)
        worker_execution_env.update(execution_env)
        execution_env = worker_execution_env
    runtime_overrides = _request_runtime_overrides(request, prepared)
    effective_runtime_paths = sandbox_exec.runtime_paths_with_execution_env(
        runtime_paths,
        execution_env,
    )
    serialized_runtime_overrides = to_json_compatible(runtime_overrides)
    prepared_request = PreparedSandboxRunnerExecuteRequest(
        tool_name=request.tool_name,
        function_name=request.function_name,
        args=list(request.args),
        kwargs=dict(request.kwargs),
        worker_key=request.worker_key,
        worker_scope=request.worker_scope,
        routing_agent_name=request.routing_agent_name,
        execution_identity=dict(request.execution_identity),
        private_agent_names=list(request.private_agent_names) if request.private_agent_names is not None else None,
        credential_overrides=dict(request.credential_overrides),
        tool_config_overrides=dict(request.tool_config_overrides),
        tool_init_overrides=_prepared_tool_init_overrides(
            request.tool_name,
            request.tool_init_overrides,
            runtime_overrides,
        ),
        runtime_overrides=(
            {name: value for name, value in serialized_runtime_overrides.items() if isinstance(name, str)}
            if isinstance(serialized_runtime_overrides, dict)
            else {}
        ),
    )
    return _PreparedSandboxRequestContext(
        request=prepared_request,
        runtime_paths=effective_runtime_paths,
        execution_env=execution_env,
        prepared_worker=prepared,
    )


def _prepare_subprocess_context(
    prepared_request: _PreparedSandboxRequestContext,
) -> _PreparedSandboxSubprocessContext:
    python_executable, subprocess_env, subprocess_cwd = sandbox_exec.resolve_subprocess_worker_context(
        prepared_request.prepared_worker.paths if prepared_request.prepared_worker is not None else None,
    )
    subprocess_env = sandbox_exec.subprocess_env_for_request(subprocess_env, prepared_request.execution_env)
    return _PreparedSandboxSubprocessContext(
        python_executable=python_executable,
        subprocess_env=subprocess_env,
        subprocess_cwd=subprocess_cwd,
    )


async def _execute_prepared_request_inprocess(
    prepared: PreparedSandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    config: Config,
) -> SandboxRunnerExecuteResponse:
    execution_identity: ToolExecutionIdentity | None = None
    if prepared.execution_identity:
        execution_identity = ToolExecutionIdentity(**prepared.execution_identity)
    private_agent_names = _freeze_private_agent_names(prepared.private_agent_names)

    with tool_execution_identity(execution_identity):
        toolkit, entrypoint = _resolve_entrypoint(
            runtime_paths=runtime_paths,
            config=config,
            tool_name=prepared.tool_name,
            function_name=prepared.function_name,
            execution_identity=execution_identity,
            credential_overrides=prepared.credential_overrides or None,
            tool_config_overrides=prepared.tool_config_overrides or None,
            tool_init_overrides=prepared.tool_init_overrides or None,
            runtime_overrides=prepared.runtime_overrides or None,
            worker_scope=prepared.worker_scope,
            routing_agent_name=prepared.routing_agent_name,
            private_agent_names=private_agent_names,
        )

        try:
            if toolkit.requires_connect:
                await _maybe_await(toolkit.connect())
                try:
                    result = await _maybe_await(entrypoint(*prepared.args, **prepared.kwargs))
                finally:
                    await _maybe_await(toolkit.close())
            else:
                result = await _maybe_await(entrypoint(*prepared.args, **prepared.kwargs))
        except Exception as exc:
            logger.warning(
                "sandbox_tool_execution_failed",
                tool_name=prepared.tool_name,
                function_name=prepared.function_name,
                exc_info=True,
            )
            return SandboxRunnerExecuteResponse(
                ok=False,
                error=f"Sandbox tool execution failed: {type(exc).__name__}: {exc}",
                failure_kind="tool",
            )

    return SandboxRunnerExecuteResponse(ok=True, result=to_json_compatible(result))


async def _execute_request_inprocess(
    request: SandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    config: Config,
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None = None,
    *,
    runner_token: str | None = None,
) -> SandboxRunnerExecuteResponse:
    try:
        prepared_request = _prepare_execute_request(
            request,
            runtime_paths,
            prepared_worker,
            runner_token=runner_token,
        )
    except sandbox_worker_prep.WorkerRequestPreparationError as exc:
        return SandboxRunnerExecuteResponse(ok=False, error=str(exc))
    effective_config = config
    if prepared_request.runtime_paths is not runtime_paths:
        effective_config = _runtime_config_or_empty(prepared_request.runtime_paths)
    return await _execute_prepared_request_inprocess(
        prepared_request.request,
        prepared_request.runtime_paths,
        effective_config,
    )


def _subprocess_failure_response(
    request: SandboxRunnerExecuteRequest | PreparedSandboxRunnerExecuteRequest,
    error: str,
    runtime_paths: RuntimePaths,
) -> SandboxRunnerExecuteResponse:
    sandbox_worker_prep.record_worker_failure(request.worker_key, error, runtime_paths)
    return SandboxRunnerExecuteResponse(ok=False, error=error, failure_kind="worker")


def _parse_subprocess_response(
    request: SandboxRunnerExecuteRequest | PreparedSandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    completed: subprocess.CompletedProcess[str],
) -> SandboxRunnerExecuteResponse:
    # The worker writes the JSON response to stderr after a marker line so that
    # tool stdout (e.g. print() inside python tools) does not corrupt the protocol.
    stderr = completed.stderr or ""
    response_json = sandbox_protocol.extract_response_json(stderr)
    if response_json:
        try:
            return SandboxRunnerExecuteResponse.model_validate_json(response_json)
        except ValidationError:
            pass

    if completed.returncode != 0:
        error = (
            stderr.strip() or completed.stdout.strip() or f"Sandbox subprocess exited with code {completed.returncode}."
        )
        return _subprocess_failure_response(request, error, runtime_paths)

    return _subprocess_failure_response(request, "Sandbox subprocess returned an invalid response.", runtime_paths)


def _execute_request_subprocess_sync(
    request: SandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None = None,
    *,
    runner_token: str | None = None,
) -> SandboxRunnerExecuteResponse:
    try:
        prepared_request = _prepare_execute_request(
            request,
            runtime_paths,
            prepared_worker,
            runner_token=runner_token,
        )
    except sandbox_worker_prep.WorkerRequestPreparationError as exc:
        return SandboxRunnerExecuteResponse(ok=False, error=str(exc))
    subprocess_context = _prepare_subprocess_context(prepared_request)
    envelope = sandbox_protocol.serialize_subprocess_envelope(
        request=prepared_request.request.model_dump(mode="json"),
        runtime_paths=constants.serialize_runtime_paths(prepared_request.runtime_paths),
    )

    try:
        completed = subprocess.run(
            sandbox_exec.subprocess_worker_command(
                _SUBPROCESS_WORKER_ARG,
                python_executable=subprocess_context.python_executable,
            ),
            input=envelope,
            capture_output=True,
            text=True,
            timeout=sandbox_exec.runner_subprocess_timeout_seconds(runtime_paths),
            check=False,
            env=subprocess_context.subprocess_env,
            cwd=subprocess_context.subprocess_cwd,
        )
    except subprocess.TimeoutExpired:
        return _subprocess_failure_response(request, "Sandbox subprocess timed out.", runtime_paths)
    except OSError as exc:
        return _subprocess_failure_response(request, f"Failed to start sandbox subprocess: {exc}", runtime_paths)

    return _parse_subprocess_response(request, runtime_paths, completed)


async def _execute_request_subprocess(
    request: SandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None = None,
    *,
    runner_token: str | None = None,
) -> SandboxRunnerExecuteResponse:
    return await asyncio.to_thread(
        _execute_request_subprocess_sync,
        request,
        runtime_paths,
        prepared_worker,
        runner_token=runner_token,
    )


def _run_subprocess_worker() -> int:
    payload = sys.stdin.read()
    if not payload.strip():
        print(
            sandbox_protocol.response_marker_payload(
                SandboxRunnerExecuteResponse(
                    ok=False,
                    error="Sandbox subprocess received empty payload.",
                    failure_kind="worker",
                ).model_dump_json(),
            ),
            file=sys.stderr,
        )
        return 1

    try:
        envelope = sandbox_protocol.parse_subprocess_envelope(payload)
        request = PreparedSandboxRunnerExecuteRequest.model_validate(envelope.request)
    except ValidationError as exc:
        print(
            sandbox_protocol.response_marker_payload(
                SandboxRunnerExecuteResponse(
                    ok=False,
                    error=f"Sandbox subprocess payload validation failed: {exc}",
                    failure_kind="worker",
                ).model_dump_json(),
            ),
            file=sys.stderr,
        )
        return 1
    runtime_paths = constants.deserialize_runtime_paths(envelope.runtime_paths)
    config = _runtime_config_or_empty(runtime_paths)

    # Redirect stdout/stderr during tool execution so tool output doesn't
    # interfere with the protocol marker we write to stderr afterwards.
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    with redirect_stdout(captured_out), redirect_stderr(captured_err):
        response = asyncio.run(_execute_prepared_request_inprocess(request, runtime_paths, config))

    # Flush captured tool output to real stdout/stderr (informational only).
    tool_stdout = captured_out.getvalue()
    if tool_stdout:
        sys.stdout.write(tool_stdout)
    tool_stderr = captured_err.getvalue()
    if tool_stderr:
        sys.stdout.write(tool_stderr)

    # Write the response JSON to stderr after the marker.
    print(sandbox_protocol.response_marker_payload(response.model_dump_json()), file=sys.stderr)
    return 0


@router.post("/leases", response_model=SandboxRunnerLeaseResponse)
async def create_credential_lease(
    request: SandboxRunnerLeaseRequest,
) -> SandboxRunnerLeaseResponse:
    """Create a short-lived, one-or-few-use credential lease."""
    lease = sandbox_worker_prep.create_credential_lease(
        tool_name=request.tool_name,
        function_name=request.function_name,
        credential_overrides=request.credential_overrides,
        ttl_seconds=request.ttl_seconds,
        max_uses=request.max_uses,
    )
    return SandboxRunnerLeaseResponse(
        lease_id=lease.lease_id,
        expires_at=lease.expires_at,
        max_uses=lease.uses_remaining,
    )


@router.get("/workers", response_model=SandboxWorkerListResponse)
async def list_workers(request: Request, include_idle: bool = True) -> SandboxWorkerListResponse:
    """List known workers and their current lifecycle status."""
    runtime_paths = sandbox_runner_runtime_paths(request)
    workers = [
        _serialize_worker(worker)
        for worker in get_local_worker_manager(runtime_paths).list_workers(include_idle=include_idle)
    ]
    return SandboxWorkerListResponse(workers=workers)


@router.post("/workers/cleanup", response_model=SandboxWorkerCleanupResponse)
async def cleanup_idle_workers(request: Request) -> SandboxWorkerCleanupResponse:
    """Mark idle workers inactive while retaining their persisted state."""
    runtime_paths = sandbox_runner_runtime_paths(request)
    worker_manager = get_local_worker_manager(runtime_paths)
    cleaned_workers = [_serialize_worker(worker) for worker in worker_manager.cleanup_idle_workers()]
    return SandboxWorkerCleanupResponse(
        idle_timeout_seconds=worker_manager.idle_timeout_seconds,
        cleaned_workers=cleaned_workers,
    )


def _validate_execute_request_payload(
    payload: SandboxRunnerExecuteRequest,
    *,
    tool_metadata: dict[str, Any],
) -> None:
    """Validate request override channels before execution dispatch."""
    if payload.credential_overrides:
        raise HTTPException(status_code=400, detail="credential_overrides must be supplied via lease_id.")
    if payload.tool_init_overrides and payload.tool_name in tool_metadata:
        try:
            payload.tool_init_overrides = (
                sanitize_tool_init_overrides(
                    payload.tool_name,
                    payload.tool_init_overrides,
                    tool_metadata=tool_metadata,
                )
                or {}
            )
        except ToolInitOverrideError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.tool_config_overrides:
        try:
            payload.tool_config_overrides = validate_authored_tool_entry_overrides(
                payload.tool_name,
                payload.tool_config_overrides,
                config_path_prefix="request.tool_config_overrides",
                tool_metadata=tool_metadata,
            )
        except ToolConfigOverrideError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.execution_env and payload.tool_name not in sandbox_exec.EXECUTION_ENV_TOOL_NAMES:
        raise HTTPException(status_code=400, detail="execution_env is only supported for execution tools.")
    if payload.extra_env_passthrough is not None and payload.tool_name != "shell":
        raise HTTPException(status_code=400, detail="extra_env_passthrough is only supported for shell.")


@router.post("/execute", response_model=SandboxRunnerExecuteResponse)
async def execute_tool_call(
    request: Request,
    payload: SandboxRunnerExecuteRequest,
) -> SandboxRunnerExecuteResponse:
    """Execute a tool function locally and return the serialized result."""
    runtime_paths = sandbox_runner_runtime_paths(request)
    config = sandbox_runner_runtime_config(request)
    tool_metadata = sandbox_runner_tool_metadata(request)
    runner_token = _app_runner_token(request.app)
    payload.worker_key = sandbox_worker_prep.normalize_request_worker_key(payload.worker_key, runtime_paths)
    _validate_execute_request_payload(payload, tool_metadata=tool_metadata)
    credential_overrides: dict[str, object] = {}
    if payload.lease_id is not None:
        credential_overrides = sandbox_worker_prep.consume_credential_lease(
            payload.lease_id,
            tool_name=payload.tool_name,
            function_name=payload.function_name,
        )

    payload.credential_overrides = credential_overrides
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None = None
    if payload.worker_key is not None:
        try:
            prepared_worker = sandbox_worker_prep.prepare_worker_request(
                worker_key=payload.worker_key,
                tool_init_overrides=payload.tool_init_overrides,
                runtime_paths=runtime_paths,
                private_agent_names=_request_private_agent_names(payload),
                runner_token=runner_token,
            )
        except sandbox_worker_prep.WorkerRequestPreparationError as exc:
            if exc.failure_kind == "worker":
                return SandboxRunnerExecuteResponse(ok=False, error=str(exc), failure_kind="worker")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Shell background handles live in the long-lived runner process, so shell
    # must stay on the in-process path even when the runner defaults to
    # per-request subprocess execution.
    if payload.tool_name != "shell" and sandbox_exec.runner_uses_subprocess(runtime_paths):
        return await _execute_request_subprocess(
            payload,
            runtime_paths,
            prepared_worker,
            runner_token=runner_token,
        )
    if payload.tool_name == "python" and sandbox_exec.request_execution_env(
        payload.tool_name,
        payload.execution_env,
        runtime_paths,
    ):
        return await _execute_request_subprocess(
            payload,
            runtime_paths,
            prepared_worker,
            runner_token=runner_token,
        )
    # Worker-routed execution stays on the subprocess path so the per-worker
    # virtualenv and worker-specific process environment remain authoritative,
    # even when this pod is itself a dedicated worker runtime.
    if payload.tool_name != "shell" and payload.worker_key is not None:
        return await _execute_request_subprocess(
            payload,
            runtime_paths,
            prepared_worker,
            runner_token=runner_token,
        )
    return await _execute_request_inprocess(
        payload,
        runtime_paths,
        config,
        prepared_worker,
        runner_token=runner_token,
    )


if __name__ == "__main__":
    if _SUBPROCESS_WORKER_ARG in sys.argv:
        raise SystemExit(_run_subprocess_worker())
