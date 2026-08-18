"""Authenticated worker transport for supervised background Python scripts."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.routing import APIRoute
from pydantic import AfterValidator, BaseModel, Field

from mindroom.api import sandbox_exec, sandbox_worker_prep
from mindroom.api.sandbox_runner import app_runner_token, app_runtime_paths, validate_runner_token
from mindroom.constants import CONTROL_STATE_PATH_ENV
from mindroom.shell_supervisor import (
    ShellSupervisorStartupError,
    check_command_via_supervisor,
    ensure_shell_supervisor,
    kill_command_via_supervisor,
    parse_shell_supervisor_status,
    run_command_via_supervisor,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from starlette.responses import Response
    from starlette.types import Message

_MAX_REQUEST_BYTES = 16 * 1024
_MAX_SOURCE_BYTES = 128 * 1024
_MAX_TOKEN_BYTES = 4096
_ALLOWED_ENVIRONMENT_NAMES = frozenset({"MINDROOM_SCRIPT_GATEWAY_URL"})
_RUN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_HANDLE_PATTERN = r"shell:[0-9a-f]{32}"
_HANDLE_RE = re.compile(r"^" + _HANDLE_PATTERN + r"$")
_LAUNCH_HANDLE_RE = re.compile(r"^Handle: (shell:[0-9a-f]{32})$", re.MULTILINE)

__all__ = [
    "SandboxScriptCancelResponse",
    "SandboxScriptControlRequest",
    "SandboxScriptRunRequest",
    "SandboxScriptRunResponse",
    "SandboxScriptStatusResponse",
    "cancel_script_in_worker",
    "router",
    "run_script_in_worker",
    "status_script_in_worker",
]


async def _bounded_replay_request(request: Request) -> Request:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc
        if content_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.")
        if content_length > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Script worker request is too large.")

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > _MAX_REQUEST_BYTES - len(body):
            raise HTTPException(status_code=413, detail="Script worker request is too large.")
        body.extend(chunk)

    original_receive = request.receive
    replayed = False

    async def replay_receive() -> Message:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}
        return await original_receive()

    return Request(request.scope, replay_receive)


class _BoundedScriptRoute(APIRoute):
    """Bound POST bodies before FastAPI performs its normal model parsing."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        route_handler = super().get_route_handler()

        async def bounded_route_handler(request: Request) -> Response:
            if request.method == "POST":
                request = await _bounded_replay_request(request)
            return await route_handler(request)

        return bounded_route_handler


def _validate_run_id(value: str) -> str:
    if re.fullmatch(_RUN_ID_PATTERN, value) is None:
        message = "Script run ID contains unsupported characters."
        raise ValueError(message)
    return value


def _validate_supervisor_handle(value: str) -> str:
    if _HANDLE_RE.fullmatch(value) is None:
        message = "Invalid script supervisor handle."
        raise ValueError(message)
    return value


_RunId = Annotated[str, AfterValidator(_validate_run_id)]
_SupervisorHandle = Annotated[str, AfterValidator(_validate_supervisor_handle)]


router = APIRouter(
    prefix="/api/sandbox-runner/scripts",
    tags=["sandbox-runner"],
    dependencies=[Depends(validate_runner_token)],
    route_class=_BoundedScriptRoute,
)


class SandboxScriptRunRequest(BaseModel):
    """Validated launch description for one already-snapshotted script."""

    run_id: _RunId
    worker_key: str = Field(min_length=1, max_length=1024)
    source_path: str = Field(min_length=1, max_length=1024)
    source_digest: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")
    token_path: str = Field(min_length=1, max_length=1024)
    supervisor_handle: _SupervisorHandle
    environment: dict[str, str] = Field(default_factory=dict, max_length=4)
    private_agent_names: list[str] | None = Field(default=None, max_length=128)
    tail_lines: int = Field(default=200, ge=1, le=1000)


class SandboxScriptControlRequest(BaseModel):
    """Worker identity and supervisor handle for status-changing operations."""

    worker_key: str = Field(min_length=1, max_length=1024)
    supervisor_handle: _SupervisorHandle
    force: bool = False


class SandboxScriptRunResponse(BaseModel):
    """Worker launch receipt."""

    ok: bool
    supervisor_handle: str | None = None
    error: str | None = None
    failure_kind: Literal["tool", "worker"] | None = None


class SandboxScriptStatusResponse(BaseModel):
    """Normalized supervisor status receipt."""

    ok: bool
    state: Literal["running", "exited", "unknown"]
    output: str = ""
    exit_code: int | None = None
    error: str | None = None
    failure_kind: Literal["tool", "worker"] | None = None


class SandboxScriptCancelResponse(BaseModel):
    """Normalized supervisor cancellation receipt."""

    ok: bool
    cancel_requested: bool
    already_finished: bool = False
    unknown_handle: bool = False
    error: str | None = None
    failure_kind: Literal["tool", "worker"] | None = None


def _script_namespace(worker_key: str, run_id: str) -> str:
    return f"script:{len(worker_key)}:{worker_key}:{run_id}"


def _normalized_worker_key(request: Request, worker_key: str) -> str:
    runtime_paths = app_runtime_paths(request.app)
    normalized_worker_key = sandbox_worker_prep.normalize_request_worker_key(worker_key, runtime_paths)
    if normalized_worker_key is None:
        raise HTTPException(status_code=400, detail="Script worker key is required.")
    dedicated_worker_key = sandbox_exec.runner_dedicated_worker_key(runtime_paths)
    if dedicated_worker_key is not None and not secrets.compare_digest(normalized_worker_key, dedicated_worker_key):
        raise HTTPException(status_code=400, detail="Worker key does not match this dedicated worker.")
    return normalized_worker_key


def _prepare_worker(
    request: Request,
    *,
    worker_key: str,
    private_agent_names: list[str] | None,
) -> sandbox_worker_prep.PreparedWorkerRequest:
    runtime_paths = app_runtime_paths(request.app)
    normalized_worker_key = _normalized_worker_key(request, worker_key)
    try:
        prepared = sandbox_worker_prep.prepare_worker_request(
            worker_key=normalized_worker_key,
            tool_init_overrides={},
            runtime_paths=runtime_paths,
            private_agent_names=(frozenset(private_agent_names) if private_agent_names is not None else None),
            runner_token=app_runner_token(request.app),
        )
    except sandbox_worker_prep.WorkerRequestPreparationError as exc:
        status_code = 503 if exc.failure_kind == "worker" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return prepared


def _workspace_file(
    workspace: Path,
    raw_relative_path: str,
    *,
    label: str,
    byte_limit: int,
) -> Path:
    relative_path = Path(raw_relative_path)
    if relative_path.is_absolute() or relative_path.is_symlink():
        raise HTTPException(status_code=400, detail=f"{label} must be a regular file inside the worker workspace.")
    candidate = workspace / relative_path
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail=f"{label} must not be a symbolic link.")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"{label} is unavailable in the worker workspace.") from exc
    if not resolved.is_relative_to(workspace.resolve()) or not stat.S_ISREG(metadata.st_mode):
        raise HTTPException(status_code=400, detail=f"{label} must be a regular file inside the worker workspace.")
    if metadata.st_size <= 0 or metadata.st_size > byte_limit:
        raise HTTPException(status_code=400, detail=f"{label} exceeds its supported size.")
    return resolved


def _validated_environment(payload: SandboxScriptRunRequest, *, workspace: Path, token_path: Path) -> dict[str, str]:
    unexpected = sorted(set(payload.environment) - _ALLOWED_ENVIRONMENT_NAMES)
    if unexpected:
        raise HTTPException(status_code=400, detail=f"Script environment contains unsupported names: {unexpected}")
    gateway_url = payload.environment.get("MINDROOM_SCRIPT_GATEWAY_URL", "")
    parsed_url = urlsplit(gateway_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or len(gateway_url) > 2048
    ):
        raise HTTPException(status_code=400, detail="Script gateway environment must contain a safe HTTP(S) URL.")
    return {
        "MINDROOM_SCRIPT_GATEWAY_URL": gateway_url.rstrip("/"),
        "MINDROOM_SCRIPT_RUN_ID": payload.run_id,
        "MINDROOM_SCRIPT_SOURCE_DIGEST": payload.source_digest,
        "MINDROOM_SCRIPT_TOKEN_PATH": str(token_path),
        "MINDROOM_SCRIPT_WORKSPACE_ROOT": str(workspace),
    }


def _validate_source_digest(source_path: Path, expected_digest: str) -> None:
    actual_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if not secrets.compare_digest(actual_digest, expected_digest):
        raise HTTPException(status_code=400, detail="Script source digest does not match the launch receipt.")


def _parse_launch_message(message: str) -> SandboxScriptRunResponse:
    match = _LAUNCH_HANDLE_RE.search(message)
    if match is not None:
        return SandboxScriptRunResponse(ok=True, supervisor_handle=match.group(1))
    return SandboxScriptRunResponse(ok=False, error=message, failure_kind="worker")


def _parse_status_message(message: str) -> SandboxScriptStatusResponse:
    status = parse_shell_supervisor_status(message)
    if status.state == "running":
        return SandboxScriptStatusResponse(ok=True, state="running", output=status.output)
    if status.state == "exited":
        return SandboxScriptStatusResponse(
            ok=True,
            state="exited",
            output=status.output,
            exit_code=status.exit_code,
        )
    if status.state == "unknown":
        return SandboxScriptStatusResponse(ok=True, state="unknown")
    return SandboxScriptStatusResponse(ok=False, state="unknown", error=status.output, failure_kind="worker")


def _parse_cancel_message(message: str) -> SandboxScriptCancelResponse:
    if message.startswith(("Terminated process", "Force-killed process")):
        return SandboxScriptCancelResponse(ok=True, cancel_requested=True)
    if message.startswith(("Process already finished", "Process ")):
        return SandboxScriptCancelResponse(ok=True, cancel_requested=False, already_finished=True)
    if message.startswith("Error: Unknown handle"):
        return SandboxScriptCancelResponse(ok=True, cancel_requested=False, unknown_handle=True)
    return SandboxScriptCancelResponse(
        ok=False,
        cancel_requested=False,
        error=message,
        failure_kind="worker",
    )


@router.post("/run", response_model=SandboxScriptRunResponse)
async def run_script_in_worker(request: Request, payload: SandboxScriptRunRequest) -> SandboxScriptRunResponse:
    """Launch one verified source snapshot under the existing shell supervisor."""
    prepared = _prepare_worker(
        request,
        worker_key=payload.worker_key,
        private_agent_names=payload.private_agent_names,
    )
    workspace = prepared.paths.workspace.resolve()
    source_path = _workspace_file(
        workspace,
        payload.source_path,
        label="Script source",
        byte_limit=_MAX_SOURCE_BYTES,
    )
    token_path = _workspace_file(
        workspace,
        payload.token_path,
        label="Script capability file",
        byte_limit=_MAX_TOKEN_BYTES,
    )
    _validate_source_digest(source_path, payload.source_digest)
    script_environment = _validated_environment(payload, workspace=workspace, token_path=token_path)
    python_executable, base_environment, _cwd = sandbox_exec.resolve_subprocess_worker_context(prepared.paths)
    if python_executable is None or base_environment is None:
        return SandboxScriptRunResponse(ok=False, error="Worker Python runtime is unavailable.", failure_kind="worker")
    execution_environment = sandbox_exec.request_execution_env("python", None, app_runtime_paths(request.app))
    environment = {**base_environment, **execution_environment, **script_environment}
    environment.pop(CONTROL_STATE_PATH_ENV, None)
    try:
        socket_path = ensure_shell_supervisor()
    except ShellSupervisorStartupError as exc:
        return SandboxScriptRunResponse(ok=False, error=str(exc), failure_kind="worker")
    message = await run_command_via_supervisor(
        socket_path,
        namespace=_script_namespace(payload.worker_key, payload.run_id),
        argv=[python_executable, "-m", "mindroom.script_runs.shim", str(source_path), str(token_path)],
        env=environment,
        cwd=str(workspace),
        tail=payload.tail_lines,
        timeout=0,
        handle=payload.supervisor_handle,
    )
    return _parse_launch_message(message)


@router.get("/{run_id}", response_model=SandboxScriptStatusResponse)
async def status_script_in_worker(
    request: Request,
    run_id: _RunId,
    worker_key: Annotated[str, Query(min_length=1, max_length=1024)],
    supervisor_handle: Annotated[_SupervisorHandle, Query()],
) -> SandboxScriptStatusResponse:
    """Poll one namespaced supervisor handle without owning lifecycle state."""
    normalized_worker_key = _normalized_worker_key(request, worker_key)
    socket_path = ensure_shell_supervisor()
    message = await asyncio.to_thread(
        check_command_via_supervisor,
        socket_path,
        namespace=_script_namespace(normalized_worker_key, run_id),
        handle=supervisor_handle,
    )
    return _parse_status_message(message)


@router.post("/{run_id}/cancel", response_model=SandboxScriptCancelResponse)
async def cancel_script_in_worker(
    request: Request,
    run_id: _RunId,
    payload: SandboxScriptControlRequest,
) -> SandboxScriptCancelResponse:
    """Signal one namespaced supervisor handle without changing durable desired state."""
    normalized_worker_key = _normalized_worker_key(request, payload.worker_key)
    socket_path = ensure_shell_supervisor()
    message = await asyncio.to_thread(
        kill_command_via_supervisor,
        socket_path,
        namespace=_script_namespace(normalized_worker_key, run_id),
        handle=payload.supervisor_handle,
        force=payload.force,
    )
    return _parse_cancel_message(message)
