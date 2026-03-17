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
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from mindroom import constants
from mindroom.api import sandbox_exec, sandbox_protocol, sandbox_worker_prep
from mindroom.config.main import Config, load_config
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    ToolInitOverrideError,
    ensure_tool_registry_loaded,
    get_tool_by_name,
    sanitize_tool_init_overrides,
)
from mindroom.tool_system.sandbox_proxy import to_json_compatible
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
    from mindroom.workers.models import WorkerHandle

_SUBPROCESS_WORKER_ARG = "--sandbox-subprocess-worker"
_STARTUP_RUNTIME_PATHS_ENV = "MINDROOM_RUNTIME_PATHS_JSON"
_RUNNER_TOKEN_ENV = "MINDROOM_SANDBOX_PROXY_TOKEN"  # noqa: S105


def _startup_runtime_paths_from_env() -> RuntimePaths:
    """Read the committed sandbox-runner runtime payload from startup env."""
    raw_payload = os.environ.get(_STARTUP_RUNTIME_PATHS_ENV, "").strip()
    if not raw_payload:
        msg = f"{_STARTUP_RUNTIME_PATHS_ENV} must be set for sandbox runner startup."
        raise RuntimeError(msg)
    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        msg = f"{_STARTUP_RUNTIME_PATHS_ENV} must contain a JSON object."
        raise TypeError(msg)
    if not isinstance(payload.get("process_env"), dict):
        msg = f"{_STARTUP_RUNTIME_PATHS_ENV} is missing process_env."
        raise TypeError(msg)
    startup_runtime_paths = constants.deserialize_runtime_paths(payload)
    process_env = dict(startup_runtime_paths.process_env)
    process_env.update(
        {key: value for key, value in os.environ.items() if key not in {_RUNNER_TOKEN_ENV, _STARTUP_RUNTIME_PATHS_ENV}},
    )
    resolved_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=startup_runtime_paths.config_path,
        storage_path=startup_runtime_paths.storage_root,
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


def _runtime_config_or_empty(runtime_paths: RuntimePaths) -> Config:
    """Return the active runtime config, or an explicit empty config if none exists."""
    if runtime_paths.config_path.exists():
        return load_config(runtime_paths)
    return Config.validate_with_runtime({}, runtime_paths)


def _load_config_from_startup_runtime() -> tuple[RuntimePaths, Config]:
    """Read the sandbox runner runtime context from explicit startup payload."""
    runtime_paths = _startup_runtime_paths_from_env()
    return runtime_paths, _runtime_config_or_empty(runtime_paths)


def initialize_sandbox_runner_app(
    api_app: FastAPI,
    runtime_paths: RuntimePaths,
    *,
    runner_token: str | None = None,
) -> None:
    """Attach one explicit runtime context to a sandbox-runner app instance."""
    api_app.state.sandbox_runner_context = _SandboxRunnerContext(
        runtime_paths=runtime_paths,
        runner_token=runner_token or sandbox_proxy.sandbox_proxy_config(runtime_paths).proxy_token,
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
    if request.private_agent_names is None:
        return None
    return frozenset(request.private_agent_names)


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
    tool_init_overrides: dict[str, Any] = Field(default_factory=dict)
    execution_env: dict[str, str] = Field(default_factory=dict)


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
    runner_token: str | None


def _app_context(app: FastAPI) -> _SandboxRunnerContext:
    context = getattr(app.state, "sandbox_runner_context", None)
    if not isinstance(context, _SandboxRunnerContext):
        msg = "Sandbox runner context is not initialized"
        raise TypeError(msg)
    return context


def _app_runtime_paths(app: FastAPI) -> RuntimePaths:
    return _app_context(app).runtime_paths


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
            tool_init_overrides=tool_init_overrides,
            runtime_overrides=runtime_overrides,
            worker_target=worker_target,
        )
    except ToolInitOverrideError as exc:
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


async def _execute_request_inprocess(
    request: SandboxRunnerExecuteRequest,
    runtime_paths: RuntimePaths,
    config: Config,
    prepared_worker: sandbox_worker_prep.PreparedWorkerRequest | None = None,
    *,
    runner_token: str | None = None,
) -> SandboxRunnerExecuteResponse:
    execution_env = sandbox_exec.request_execution_env(request.tool_name, request.execution_env, runtime_paths)
    try:
        prepared = sandbox_worker_prep.resolve_prepared_worker_request(
            worker_key=request.worker_key,
            tool_init_overrides=request.tool_init_overrides,
            runtime_paths=runtime_paths,
            private_agent_names=_request_private_agent_names(request),
            prepared_worker=prepared_worker,
            runner_token=runner_token,
        )
    except sandbox_worker_prep.WorkerRequestPreparationError as exc:
        return SandboxRunnerExecuteResponse(ok=False, error=str(exc))
    runtime_overrides = sandbox_worker_prep.ready_runtime_overrides(
        prepared.runtime_overrides if prepared is not None else None,
    )
    effective_runtime_paths = sandbox_exec.runtime_paths_with_execution_env(
        runtime_paths,
        execution_env,
    )
    execution_identity: ToolExecutionIdentity | None = None
    if request.execution_identity:
        execution_identity = ToolExecutionIdentity(**request.execution_identity)
    effective_config = config
    if effective_runtime_paths is not runtime_paths:
        effective_config = _runtime_config_or_empty(effective_runtime_paths)

    with tool_execution_identity(execution_identity):
        toolkit, entrypoint = _resolve_entrypoint(
            runtime_paths=effective_runtime_paths,
            config=effective_config,
            tool_name=request.tool_name,
            function_name=request.function_name,
            execution_identity=execution_identity,
            credential_overrides=request.credential_overrides or None,
            tool_init_overrides=request.tool_init_overrides or None,
            runtime_overrides=runtime_overrides,
            worker_scope=request.worker_scope,
            routing_agent_name=request.routing_agent_name,
            private_agent_names=_request_private_agent_names(request),
        )

        try:
            if toolkit.requires_connect:
                await _maybe_await(toolkit.connect())
                try:
                    result = await _maybe_await(entrypoint(*request.args, **request.kwargs))
                finally:
                    await _maybe_await(toolkit.close())
            else:
                result = await _maybe_await(entrypoint(*request.args, **request.kwargs))
        except Exception as exc:
            logger.opt(exception=True).warning(
                f"Sandbox tool execution failed: {request.tool_name}.{request.function_name}",
            )
            return SandboxRunnerExecuteResponse(
                ok=False,
                error=f"Sandbox tool execution failed: {type(exc).__name__}: {exc}",
            )

    return SandboxRunnerExecuteResponse(ok=True, result=to_json_compatible(result))


def _subprocess_failure_response(
    request: SandboxRunnerExecuteRequest,
    error: str,
    runtime_paths: RuntimePaths,
) -> SandboxRunnerExecuteResponse:
    sandbox_worker_prep.record_worker_failure(request.worker_key, error, runtime_paths)
    return SandboxRunnerExecuteResponse(ok=False, error=error)


def _parse_subprocess_response(
    request: SandboxRunnerExecuteRequest,
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
    execution_env = sandbox_exec.request_execution_env(request.tool_name, request.execution_env, runtime_paths)
    try:
        prepared = sandbox_worker_prep.resolve_prepared_worker_request(
            worker_key=request.worker_key,
            tool_init_overrides=request.tool_init_overrides,
            runtime_paths=runtime_paths,
            private_agent_names=_request_private_agent_names(request),
            prepared_worker=prepared_worker,
            runner_token=runner_token,
        )
    except sandbox_worker_prep.WorkerRequestPreparationError as exc:
        return SandboxRunnerExecuteResponse(ok=False, error=str(exc))

    python_executable, subprocess_env, cwd = sandbox_exec.resolve_subprocess_worker_context(
        prepared.paths if prepared is not None else None,
    )
    subprocess_env = sandbox_exec.subprocess_env_for_request(subprocess_env, execution_env)
    envelope = sandbox_protocol.serialize_subprocess_envelope(
        request=request.model_dump(mode="json"),
        runtime_paths=constants.serialize_runtime_paths(runtime_paths),
    )

    try:
        completed = subprocess.run(
            sandbox_exec.subprocess_worker_command(_SUBPROCESS_WORKER_ARG, python_executable=python_executable),
            input=envelope,
            capture_output=True,
            text=True,
            timeout=sandbox_exec.runner_subprocess_timeout_seconds(runtime_paths),
            check=False,
            env=subprocess_env,
            cwd=cwd,
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
                ).model_dump_json(),
            ),
            file=sys.stderr,
        )
        return 1

    try:
        envelope = sandbox_protocol.parse_subprocess_envelope(payload)
        request = SandboxRunnerExecuteRequest.model_validate(envelope.request)
    except ValidationError as exc:
        print(
            sandbox_protocol.response_marker_payload(
                SandboxRunnerExecuteResponse(
                    ok=False,
                    error=f"Sandbox subprocess payload validation failed: {exc}",
                ).model_dump_json(),
            ),
            file=sys.stderr,
        )
        return 1
    runtime_paths = constants.deserialize_runtime_paths(envelope.runtime_paths)
    request.worker_key = sandbox_worker_prep.normalize_request_worker_key(request.worker_key, runtime_paths)
    config = _runtime_config_or_empty(runtime_paths)

    # Redirect stdout/stderr during tool execution so tool output doesn't
    # interfere with the protocol marker we write to stderr afterwards.
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    with redirect_stdout(captured_out), redirect_stderr(captured_err):
        response = asyncio.run(_execute_request_inprocess(request, runtime_paths, config))

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


@router.post("/execute", response_model=SandboxRunnerExecuteResponse)
async def execute_tool_call(  # noqa: C901
    request: Request,
    payload: SandboxRunnerExecuteRequest,
) -> SandboxRunnerExecuteResponse:
    """Execute a tool function locally and return the serialized result."""
    runtime_paths = sandbox_runner_runtime_paths(request)
    config = _runtime_config_or_empty(runtime_paths)
    runner_token = _app_runner_token(request.app)
    payload.worker_key = sandbox_worker_prep.normalize_request_worker_key(payload.worker_key, runtime_paths)
    if payload.credential_overrides:
        raise HTTPException(status_code=400, detail="credential_overrides must be supplied via lease_id.")
    if payload.tool_init_overrides and payload.tool_name in TOOL_METADATA:
        try:
            payload.tool_init_overrides = (
                sanitize_tool_init_overrides(payload.tool_name, payload.tool_init_overrides) or {}
            )
        except ToolInitOverrideError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    credential_overrides: dict[str, object] = {}
    if payload.lease_id is not None:
        credential_overrides = sandbox_worker_prep.consume_credential_lease(
            payload.lease_id,
            tool_name=payload.tool_name,
            function_name=payload.function_name,
        )

    payload.credential_overrides = credential_overrides
    if payload.execution_env and payload.tool_name not in sandbox_exec.EXECUTION_ENV_TOOL_NAMES:
        raise HTTPException(status_code=400, detail="execution_env is only supported for execution tools.")
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
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if sandbox_exec.runner_uses_subprocess(runtime_paths):
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
    if payload.worker_key is not None:
        return await _execute_request_subprocess(
            payload,
            runtime_paths,
            prepared_worker,
            runner_token=runner_token,
        )
    return await _execute_request_inprocess(payload, runtime_paths, config, prepared_worker, runner_token=runner_token)


if __name__ == "__main__":
    if _SUBPROCESS_WORKER_ARG in sys.argv:
        raise SystemExit(_run_subprocess_worker())
