"""Sandbox runner API for executing tool calls inside isolated containers."""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import secrets
import site
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

import mindroom.tool_system.sandbox_proxy as _sandbox_proxy
from mindroom import constants
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    ToolInitOverrideError,
    ensure_tool_registry_loaded,
    get_tool_by_name,
    sanitize_tool_init_overrides,
)
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_token_matches, to_json_compatible
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    tool_execution_identity,
    visible_agent_state_roots_for_worker_key,
    worker_dir_name,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.local import (
    LOCAL_WORKER_ROOT_ENV,
    LocalWorkerStatePaths,
    ensure_local_worker_state_locked,
    get_local_worker_manager,
    local_worker_state_paths_for_root,
    local_worker_state_paths_from_handle,
)
from mindroom.workers.models import WorkerHandle, WorkerSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools.toolkit import Toolkit

    from mindroom.config.main import Config

_MAX_LEASE_TTL_SECONDS = 3600
_DEFAULT_LEASE_TTL_SECONDS = 60
_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 120.0
_SUBPROCESS_WORKER_ARG = "--sandbox-subprocess-worker"
_RUNNER_EXECUTION_MODE_ENV = "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE"
_RUNNER_SUBPROCESS_TIMEOUT_ENV = "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS"
_DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
_DEDICATED_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"
_SHARED_STORAGE_ROOT_ENV = "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT"
_KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV = "MINDROOM_KUBERNETES_WORKER_STORAGE_SUBPATH_PREFIX"
_DEFAULT_WORKER_STORAGE_SUBPATH_PREFIX = "workers"

# Sentinel written to stderr to delimit the JSON response from tool output.
_RESPONSE_MARKER = "__SANDBOX_RESPONSE__"


def _load_config_from_env() -> tuple[Config | None, Path | None]:
    """Read runner config path from environment variables."""
    from mindroom.config.main import Config as _Config  # noqa: PLC0415
    from mindroom.constants import find_config  # noqa: PLC0415

    config_path = find_config()
    if config_path.exists():
        return _Config.from_yaml(config_path), config_path
    return None, None


def ensure_registry_loaded_with_config() -> None:
    """Load config from env and ensure the tool registry is populated.

    Used by both the FastAPI startup and the subprocess worker so that
    plugin tools are registered even in fresh processes.
    """
    config, config_path = _load_config_from_env()
    ensure_tool_registry_loaded(config, config_path=config_path)


@dataclass
class _CredentialLease:
    """In-memory lease for short-lived credential overrides."""

    lease_id: str
    tool_name: str
    function_name: str
    credential_overrides: dict[str, Any]
    expires_at: float
    uses_remaining: int


# NOTE: In-process dict — leases are not shared across multiple uvicorn workers.
# The sandbox runner must be deployed with a single worker for lease correctness.
_LEASES_BY_ID: dict[str, _CredentialLease] = {}
_LEASES_LOCK = threading.Lock()


class SandboxRunnerExecuteRequest(BaseModel):
    """Tool call payload forwarded from a primary runtime to the sandbox runtime.

    Clients must provide credentials via ``lease_id``.
    ``credential_overrides`` is reserved for internal in-process and subprocess
    execution after the lease has been resolved.
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
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
    tool_init_overrides: dict[str, Any] = Field(default_factory=dict)


class SandboxRunnerLeaseRequest(BaseModel):
    """Request for creating a short-lived credential lease."""

    tool_name: str
    function_name: str
    credential_overrides: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS
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
class _PreparedWorkerRequest:
    handle: WorkerHandle
    paths: LocalWorkerStatePaths
    runtime_overrides: dict[str, object]


class _WorkerRequestPreparationError(ValueError):
    """Raised when one worker-backed execute request cannot be prepared."""


async def _validate_runner_token(x_mindroom_sandbox_token: Annotated[str | None, Header()] = None) -> None:
    if _sandbox_proxy._PROXY_TOKEN is None:
        raise HTTPException(status_code=503, detail="Sandbox runner token is not configured.")
    if not sandbox_proxy_token_matches(x_mindroom_sandbox_token):
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
    tool_name: str,
    function_name: str,
    credential_overrides: dict[str, object] | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    runtime_overrides: dict[str, object] | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
) -> tuple[Toolkit, Callable[..., object]]:
    ensure_registry_loaded_with_config()
    try:
        toolkit = get_tool_by_name(
            tool_name,
            disable_sandbox_proxy=True,
            credential_overrides=credential_overrides,
            tool_init_overrides=tool_init_overrides,
            runtime_overrides=runtime_overrides,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
        )
    except ToolInitOverrideError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    function = toolkit.functions.get(function_name) or toolkit.async_functions.get(function_name)
    if function is None or function.entrypoint is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' does not expose '{function_name}'.")
    return toolkit, function.entrypoint


def _bounded_ttl_seconds(raw_ttl_seconds: int) -> int:
    return max(1, min(_MAX_LEASE_TTL_SECONDS, raw_ttl_seconds))


def _bounded_max_uses(raw_max_uses: int) -> int:
    return max(1, min(10, raw_max_uses))


def _cleanup_expired_leases(now: float) -> None:
    expired_ids = [lease_id for lease_id, lease in _LEASES_BY_ID.items() if lease.expires_at <= now]
    for lease_id in expired_ids:
        _LEASES_BY_ID.pop(lease_id, None)


def _create_credential_lease(request: SandboxRunnerLeaseRequest) -> _CredentialLease:
    ttl_seconds = _bounded_ttl_seconds(request.ttl_seconds)
    max_uses = _bounded_max_uses(request.max_uses)
    now = time.time()
    expires_at = now + ttl_seconds
    lease = _CredentialLease(
        lease_id=secrets.token_urlsafe(24),
        tool_name=request.tool_name,
        function_name=request.function_name,
        credential_overrides=dict(request.credential_overrides),
        expires_at=expires_at,
        uses_remaining=max_uses,
    )
    with _LEASES_LOCK:
        _cleanup_expired_leases(now)
        _LEASES_BY_ID[lease.lease_id] = lease
    return lease


def _consume_credential_lease(lease_id: str, *, tool_name: str, function_name: str) -> dict[str, object]:
    now = time.time()
    with _LEASES_LOCK:
        _cleanup_expired_leases(now)
        lease = _LEASES_BY_ID.get(lease_id)
        if lease is None:
            raise HTTPException(status_code=400, detail="Credential lease is invalid or expired.")
        if lease.tool_name != tool_name or lease.function_name != function_name:
            raise HTTPException(status_code=400, detail="Credential lease does not match tool/function.")

        lease.uses_remaining -= 1
        if lease.uses_remaining <= 0:
            _LEASES_BY_ID.pop(lease_id, None)

    return dict(lease.credential_overrides)


def _runner_execution_mode() -> str:
    return os.getenv(_RUNNER_EXECUTION_MODE_ENV, "inprocess").strip().lower()


def _runner_uses_subprocess() -> bool:
    return _runner_execution_mode() == "subprocess"


def _runner_subprocess_timeout_seconds() -> float:
    raw_timeout = os.getenv(_RUNNER_SUBPROCESS_TIMEOUT_ENV, str(_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS))
    try:
        timeout = float(raw_timeout)
    except ValueError:
        timeout = _DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    return max(1.0, timeout)


def _runner_dedicated_worker_key() -> str | None:
    raw = os.getenv(_DEDICATED_WORKER_KEY_ENV, "").strip()
    return raw or None


def _runner_dedicated_worker_root() -> Path | None:
    dedicated_root = os.getenv(_DEDICATED_WORKER_ROOT_ENV, "").strip()
    if dedicated_root:
        return Path(dedicated_root).expanduser().resolve()

    storage_root = os.getenv("MINDROOM_STORAGE_PATH", "").strip()
    if storage_root:
        return Path(storage_root).expanduser().resolve()
    return None


def _runner_shared_storage_root() -> Path | None:
    shared_root = os.getenv(_SHARED_STORAGE_ROOT_ENV, "").strip()
    if shared_root:
        return Path(shared_root).expanduser().resolve()

    dedicated_root = _runner_dedicated_worker_root()
    worker_key = _runner_dedicated_worker_key()
    if dedicated_root is None or worker_key is None:
        return None

    storage_subpath_prefix = (
        os.getenv(
            _KUBERNETES_STORAGE_SUBPATH_PREFIX_ENV,
            _DEFAULT_WORKER_STORAGE_SUBPATH_PREFIX,
        ).strip()
        or _DEFAULT_WORKER_STORAGE_SUBPATH_PREFIX
    )
    return _shared_root_from_dedicated_worker_root(
        dedicated_root=dedicated_root,
        worker_key=worker_key,
        storage_subpath_prefix=storage_subpath_prefix,
    )


def _shared_root_from_dedicated_worker_root(
    *,
    dedicated_root: Path,
    worker_key: str,
    storage_subpath_prefix: str,
) -> Path | None:
    """Recover the shared storage root from `<shared>/<prefix>/<worker-dir>`."""
    resolved_dedicated_root = dedicated_root.expanduser().resolve()
    if resolved_dedicated_root.name != worker_dir_name(worker_key):
        return None

    prefix_parts = tuple(Path(storage_subpath_prefix.strip("/")).parts)
    parent = resolved_dedicated_root.parent
    for expected_part in reversed(prefix_parts):
        if parent.name != expected_part:
            return None
        parent = parent.parent
    return parent.resolve()


def _runner_storage_root() -> Path:
    if shared_root := _runner_shared_storage_root():
        return shared_root

    storage_root = os.getenv("MINDROOM_STORAGE_PATH", "").strip()
    if storage_root:
        return Path(storage_root).expanduser().resolve()

    return constants.STORAGE_PATH_OBJ.resolve()


def _runner_uses_dedicated_worker() -> bool:
    return _runner_dedicated_worker_key() is not None


def _project_src_path() -> Path:
    return Path(__file__).resolve().parents[2]


def _current_runtime_site_packages() -> list[str]:
    site_package_paths = list(site.getsitepackages())
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        site_package_paths.append(user_site)

    discovered_paths: list[str] = []
    for path_text in site_package_paths:
        path = Path(path_text).expanduser()
        if path.is_dir():
            discovered_paths.append(str(path.resolve()))

    return list(dict.fromkeys(discovered_paths))


def _worker_subprocess_env(paths: LocalWorkerStatePaths) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(paths.root)
    current_storage_root = env.get("MINDROOM_STORAGE_PATH", "").strip()
    env["MINDROOM_STORAGE_PATH"] = current_storage_root or str(_runner_storage_root())
    env[LOCAL_WORKER_ROOT_ENV] = str(paths.root.parent)
    env["XDG_CACHE_HOME"] = str(paths.cache_dir)
    env["PIP_CACHE_DIR"] = str(paths.cache_dir / "pip")
    env["UV_CACHE_DIR"] = str(paths.cache_dir / "uv")
    env["PYTHONPYCACHEPREFIX"] = str(paths.cache_dir / "pycache")
    env["VIRTUAL_ENV"] = str(paths.venv_dir)

    current_path = env.get("PATH", "")
    env["PATH"] = f"{paths.venv_dir / 'bin'}:{current_path}" if current_path else str(paths.venv_dir / "bin")

    project_src = str(_project_src_path())
    python_path_parts = [project_src, *_current_runtime_site_packages()]
    existing_python_path = env.get("PYTHONPATH", "")
    if existing_python_path:
        python_path_parts.append(existing_python_path)
    env["PYTHONPATH"] = ":".join(python_path_parts)
    dedicated_worker_key = _runner_dedicated_worker_key()
    if dedicated_worker_key is not None:
        env[_DEDICATED_WORKER_KEY_ENV] = dedicated_worker_key
        env[_DEDICATED_WORKER_ROOT_ENV] = str(paths.root)
    return env


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


def _prepare_worker(worker_key: str) -> WorkerHandle:
    dedicated_worker_key = _runner_dedicated_worker_key()
    if dedicated_worker_key is not None:
        if worker_key != dedicated_worker_key:
            msg = f"Dedicated sandbox worker is pinned to '{dedicated_worker_key}' but received '{worker_key}'."
            raise WorkerBackendError(msg)
        dedicated_root = _runner_dedicated_worker_root()
        if dedicated_root is None:
            msg = "Dedicated sandbox worker requires a configured worker root."
            raise WorkerBackendError(msg)
        paths = local_worker_state_paths_for_root(dedicated_root)
        try:
            ensure_local_worker_state_locked(worker_key, paths)
        except Exception as exc:
            failure_reason = f"Failed to initialize dedicated worker '{worker_key}': {exc}"
            raise WorkerBackendError(failure_reason) from exc
        now = time.time()
        return WorkerHandle(
            worker_id=worker_dir_name(worker_key),
            worker_key=worker_key,
            endpoint="/api/sandbox-runner/execute",
            auth_token=_sandbox_proxy._PROXY_TOKEN,
            status="ready",
            backend_name="dedicated_sandbox_runner",
            last_used_at=now,
            created_at=now,
            last_started_at=now,
            startup_count=1,
            debug_metadata={
                "state_root": str(paths.root),
                "api_root": "/api/sandbox-runner",
            },
        )
    return get_local_worker_manager().ensure_worker(WorkerSpec(worker_key))


def _normalize_request_worker_key(request: SandboxRunnerExecuteRequest) -> SandboxRunnerExecuteRequest:
    """Fill in the pinned worker key for dedicated worker pods when omitted."""
    dedicated_worker_key = _runner_dedicated_worker_key()
    if dedicated_worker_key is not None and request.worker_key is None:
        request.worker_key = dedicated_worker_key
    return request


def _resolve_worker_base_dir(
    paths: LocalWorkerStatePaths,
    storage_root: Path,
    worker_key: str,
    requested_base_dir: object | None,
) -> Path:
    """Resolve the effective base_dir inside shared storage or the worker root."""
    shared_root = storage_root.resolve()
    if requested_base_dir is None:
        return paths.workspace.resolve()
    if not isinstance(requested_base_dir, str):
        msg = "base_dir must be a string path."
        raise TypeError(msg)

    visible_agent_roots = visible_agent_state_roots_for_worker_key(storage_root, worker_key)
    raw_path = Path(requested_base_dir).expanduser()
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    elif visible_agent_roots:
        candidate = (shared_root / raw_path).resolve()
    else:
        msg = f"base_dir requires a resolved worker key with visible agent roots: {worker_key}"
        raise ValueError(msg)

    allowed_roots = (paths.root.resolve(), *visible_agent_roots)
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        msg = f"base_dir must stay inside the allowed agent roots or worker root: {requested_base_dir}"
        raise ValueError(msg)

    return candidate


def _ready_runtime_overrides(runtime_overrides: dict[str, object] | None) -> dict[str, object] | None:
    if runtime_overrides is None:
        return None

    base_dir = runtime_overrides.get("base_dir")
    if isinstance(base_dir, Path):
        base_dir.mkdir(parents=True, exist_ok=True)
    return runtime_overrides


def _prepare_worker_request(
    request: SandboxRunnerExecuteRequest,
) -> _PreparedWorkerRequest:
    """Prepare one worker-backed request for execution."""
    if request.worker_key is None:
        msg = "worker_key is required for worker-backed sandbox execution."
        raise _WorkerRequestPreparationError(msg)

    try:
        worker_handle = _prepare_worker(request.worker_key)
    except WorkerBackendError as exc:
        logger.opt(exception=True).warning("Sandbox worker initialization failed", worker_key=request.worker_key)
        raise _WorkerRequestPreparationError(str(exc)) from exc

    paths = local_worker_state_paths_from_handle(worker_handle)
    storage_root = _runner_storage_root()
    try:
        runtime_overrides = {
            "base_dir": _resolve_worker_base_dir(
                paths,
                storage_root,
                request.worker_key,
                request.tool_init_overrides.get("base_dir"),
            ),
        }
    except (TypeError, ValueError) as exc:
        raise _WorkerRequestPreparationError(str(exc)) from exc

    return _PreparedWorkerRequest(
        handle=worker_handle,
        paths=paths,
        runtime_overrides=runtime_overrides,
    )


def _resolve_prepared_worker_request(
    request: SandboxRunnerExecuteRequest,
    prepared_worker: _PreparedWorkerRequest | None,
) -> _PreparedWorkerRequest | None:
    if request.worker_key is None:
        return None
    return prepared_worker or _prepare_worker_request(request)


async def _execute_request_inprocess(
    request: SandboxRunnerExecuteRequest,
    prepared_worker: _PreparedWorkerRequest | None = None,
) -> SandboxRunnerExecuteResponse:
    try:
        prepared = _resolve_prepared_worker_request(request, prepared_worker)
    except _WorkerRequestPreparationError as exc:
        return SandboxRunnerExecuteResponse(ok=False, error=str(exc))
    runtime_overrides = _ready_runtime_overrides(prepared.runtime_overrides if prepared is not None else None)
    execution_identity: ToolExecutionIdentity | None = None
    if request.execution_identity:
        execution_identity = ToolExecutionIdentity(**request.execution_identity)

    with tool_execution_identity(execution_identity):
        toolkit, entrypoint = _resolve_entrypoint(
            tool_name=request.tool_name,
            function_name=request.function_name,
            credential_overrides=request.credential_overrides or None,
            tool_init_overrides=request.tool_init_overrides or None,
            runtime_overrides=runtime_overrides,
            worker_scope=request.worker_scope,
            routing_agent_name=request.routing_agent_name,
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


def _subprocess_worker_command(python_executable: str | None = None) -> list[str]:
    return [python_executable or sys.executable, "-m", "mindroom.api.sandbox_runner", _SUBPROCESS_WORKER_ARG]


def _subprocess_failure_response(
    request: SandboxRunnerExecuteRequest,
    error: str,
) -> SandboxRunnerExecuteResponse:
    if request.worker_key is not None and not _runner_uses_dedicated_worker():
        get_local_worker_manager().record_failure(request.worker_key, error)
    return SandboxRunnerExecuteResponse(ok=False, error=error)


def _resolve_subprocess_worker_context(
    prepared_worker: _PreparedWorkerRequest | None,
) -> tuple[str | None, dict[str, str] | None, str | None]:
    if prepared_worker is None:
        return None, None, None

    paths = prepared_worker.paths
    return (
        str(paths.venv_dir / "bin" / "python"),
        _worker_subprocess_env(paths),
        str(paths.workspace),
    )


def _parse_subprocess_response(
    request: SandboxRunnerExecuteRequest,
    completed: subprocess.CompletedProcess[str],
) -> SandboxRunnerExecuteResponse:
    # The worker writes the JSON response to stderr after a marker line so that
    # tool stdout (e.g. print() inside python tools) does not corrupt the protocol.
    stderr = completed.stderr or ""
    marker_pos = stderr.rfind(_RESPONSE_MARKER)
    if marker_pos != -1:
        response_json = stderr[marker_pos + len(_RESPONSE_MARKER) :].strip()
        if response_json:
            try:
                return SandboxRunnerExecuteResponse.model_validate_json(response_json)
            except ValidationError:
                pass

    if completed.returncode != 0:
        error = (
            stderr.strip() or completed.stdout.strip() or f"Sandbox subprocess exited with code {completed.returncode}."
        )
        return _subprocess_failure_response(request, error)

    return _subprocess_failure_response(request, "Sandbox subprocess returned an invalid response.")


def _execute_request_subprocess_sync(
    request: SandboxRunnerExecuteRequest,
    prepared_worker: _PreparedWorkerRequest | None = None,
) -> SandboxRunnerExecuteResponse:
    try:
        prepared = _resolve_prepared_worker_request(request, prepared_worker)
    except _WorkerRequestPreparationError as exc:
        return SandboxRunnerExecuteResponse(ok=False, error=str(exc))

    python_executable, subprocess_env, cwd = _resolve_subprocess_worker_context(prepared)

    try:
        completed = subprocess.run(
            _subprocess_worker_command(python_executable),
            input=request.model_dump_json(),
            capture_output=True,
            text=True,
            timeout=_runner_subprocess_timeout_seconds(),
            check=False,
            env=subprocess_env,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _subprocess_failure_response(request, "Sandbox subprocess timed out.")
    except OSError as exc:
        return _subprocess_failure_response(request, f"Failed to start sandbox subprocess: {exc}")

    return _parse_subprocess_response(request, completed)


async def _execute_request_subprocess(
    request: SandboxRunnerExecuteRequest,
    prepared_worker: _PreparedWorkerRequest | None = None,
) -> SandboxRunnerExecuteResponse:
    return await asyncio.to_thread(_execute_request_subprocess_sync, request, prepared_worker)


def _run_subprocess_worker() -> int:
    payload = sys.stdin.read()
    if not payload.strip():
        print(
            _RESPONSE_MARKER
            + SandboxRunnerExecuteResponse(
                ok=False,
                error="Sandbox subprocess received empty payload.",
            ).model_dump_json(),
            file=sys.stderr,
        )
        return 1

    try:
        request = SandboxRunnerExecuteRequest.model_validate_json(payload)
    except ValidationError as exc:
        print(
            _RESPONSE_MARKER
            + SandboxRunnerExecuteResponse(
                ok=False,
                error=f"Sandbox subprocess payload validation failed: {exc}",
            ).model_dump_json(),
            file=sys.stderr,
        )
        return 1
    request = _normalize_request_worker_key(request)

    # Redirect stdout/stderr during tool execution so tool output doesn't
    # interfere with the protocol marker we write to stderr afterwards.
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    with redirect_stdout(captured_out), redirect_stderr(captured_err):
        response = asyncio.run(_execute_request_inprocess(request))

    # Flush captured tool output to real stdout/stderr (informational only).
    tool_stdout = captured_out.getvalue()
    if tool_stdout:
        sys.stdout.write(tool_stdout)
    tool_stderr = captured_err.getvalue()
    if tool_stderr:
        sys.stdout.write(tool_stderr)

    # Write the response JSON to stderr after the marker.
    print(_RESPONSE_MARKER + response.model_dump_json(), file=sys.stderr)
    return 0


@router.post("/leases", response_model=SandboxRunnerLeaseResponse)
async def create_credential_lease(
    request: SandboxRunnerLeaseRequest,
) -> SandboxRunnerLeaseResponse:
    """Create a short-lived, one-or-few-use credential lease."""
    lease = _create_credential_lease(request)
    return SandboxRunnerLeaseResponse(
        lease_id=lease.lease_id,
        expires_at=lease.expires_at,
        max_uses=lease.uses_remaining,
    )


@router.get("/workers", response_model=SandboxWorkerListResponse)
async def list_workers(include_idle: bool = True) -> SandboxWorkerListResponse:
    """List known workers and their current lifecycle status."""
    workers = [
        _serialize_worker(worker) for worker in get_local_worker_manager().list_workers(include_idle=include_idle)
    ]
    return SandboxWorkerListResponse(workers=workers)


@router.post("/workers/cleanup", response_model=SandboxWorkerCleanupResponse)
async def cleanup_idle_workers() -> SandboxWorkerCleanupResponse:
    """Mark idle workers inactive while retaining their persisted state."""
    worker_manager = get_local_worker_manager()
    cleaned_workers = [_serialize_worker(worker) for worker in worker_manager.cleanup_idle_workers()]
    return SandboxWorkerCleanupResponse(
        idle_timeout_seconds=worker_manager.idle_timeout_seconds,
        cleaned_workers=cleaned_workers,
    )


@router.post("/execute", response_model=SandboxRunnerExecuteResponse)
async def execute_tool_call(
    request: SandboxRunnerExecuteRequest,
) -> SandboxRunnerExecuteResponse:
    """Execute a tool function locally and return the serialized result."""
    request = _normalize_request_worker_key(request)
    if request.credential_overrides:
        raise HTTPException(status_code=400, detail="credential_overrides must be supplied via lease_id.")
    if request.tool_init_overrides and request.tool_name in TOOL_METADATA:
        try:
            request.tool_init_overrides = (
                sanitize_tool_init_overrides(request.tool_name, request.tool_init_overrides) or {}
            )
        except ToolInitOverrideError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    credential_overrides: dict[str, object] = {}
    if request.lease_id is not None:
        credential_overrides = _consume_credential_lease(
            request.lease_id,
            tool_name=request.tool_name,
            function_name=request.function_name,
        )

    request.credential_overrides = credential_overrides
    prepared_worker: _PreparedWorkerRequest | None = None
    if request.worker_key is not None:
        try:
            prepared_worker = _prepare_worker_request(request)
        except _WorkerRequestPreparationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _runner_uses_subprocess():
        return await _execute_request_subprocess(request, prepared_worker)
    # Worker-routed execution stays on the subprocess path so the per-worker
    # virtualenv and worker-specific process environment remain authoritative,
    # even when this pod is itself a dedicated worker runtime.
    if request.worker_key is not None:
        return await _execute_request_subprocess(request, prepared_worker)
    return await _execute_request_inprocess(request, prepared_worker)


if __name__ == "__main__":
    if _SUBPROCESS_WORKER_ARG in sys.argv:
        raise SystemExit(_run_subprocess_worker())
