"""Authenticated worker transport for supervised background Python scripts."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import stat
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute

from mindroom.api import sandbox_env_assembly, sandbox_exec, sandbox_worker_prep
from mindroom.api.sandbox_runner import (
    app_runner_token,
    app_runtime_paths,
    request_runtime_config,
    resolve_script_state_workspace,
    validate_runner_token,
)
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.path_confinement import resolve_path_within_root
from mindroom.script_runs import launch_contract
from mindroom.script_runs.models import script_run_id_from_worker_key, script_worker_key_for_run
from mindroom.script_runs.supervised import (
    ScriptSnapshot,
    launch_supervised_script,
    signal_supervised_script,
    supervised_script_status,
    worker_script_namespace,
)
from mindroom.script_runs.wire import (
    SandboxScriptCancelResponse,
    SandboxScriptControlRequest,
    SandboxScriptRunRequest,
    SandboxScriptRunResponse,
    SandboxScriptStatusRequest,
    SandboxScriptStatusResponse,
    ScriptRunId,
    ScriptWorkerError,
)
from mindroom.shell.supervisor import (
    ShellSupervisorStartupError,
    background_script_supervision_supported,
    ensure_shell_supervisor,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path

    from starlette.responses import Response
    from starlette.types import Message

    from mindroom.config.main import Config

# Launch requests carry the primary's live config snapshot, which dominates their size.
_MAX_REQUEST_BYTES = 8 * 1024 * 1024

__all__ = [
    "cancel_script_in_worker",
    "prepare_script_worker_before_serving",
    "router",
    "run_script_in_worker",
    "status_script_in_worker",
]


async def prepare_script_worker_before_serving(app: FastAPI) -> None:
    """Finish cold script-worker setup before the runner advertises readiness."""
    runtime_paths = app_runtime_paths(app)
    worker_key = sandbox_exec.runner_dedicated_worker_key(runtime_paths)
    if worker_key is None:
        return
    if script_run_id_from_worker_key(worker_key) is None:
        return

    await asyncio.to_thread(
        sandbox_worker_prep.prepare_script_worker,
        worker_key,
        runtime_paths,
        runner_token=app_runner_token(app),
    )
    await asyncio.to_thread(ensure_shell_supervisor)


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

    try:
        body = await collect_bounded_bytes(request.stream(), max_bytes=_MAX_REQUEST_BYTES)
    except ByteLimitExceededError as exc:
        raise HTTPException(status_code=413, detail="Script worker request is too large.") from exc

    original_receive = request.receive
    replayed = False

    async def replay_receive() -> Message:
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
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


router = APIRouter(
    prefix="/api/sandbox-runner/scripts",
    tags=["sandbox-runner"],
    dependencies=[Depends(validate_runner_token)],
    route_class=_BoundedScriptRoute,
)


def _require_dedicated_runner(request: Request) -> str:
    dedicated_worker_key = sandbox_exec.runner_dedicated_worker_key(app_runtime_paths(request.app))
    if dedicated_worker_key is None:
        raise HTTPException(status_code=404, detail="Script runner routes require a dedicated worker.")
    return dedicated_worker_key


def _normalized_worker_key(request: Request, worker_key: str) -> str:
    runtime_paths = app_runtime_paths(request.app)
    normalized_worker_key = sandbox_worker_prep.normalize_request_worker_key(worker_key, runtime_paths)
    if normalized_worker_key is None:
        raise HTTPException(status_code=400, detail="Script worker key is required.")
    dedicated_worker_key = _require_dedicated_runner(request)
    if not secrets.compare_digest(normalized_worker_key, dedicated_worker_key):
        raise HTTPException(status_code=400, detail="Worker key does not match this dedicated worker.")
    return normalized_worker_key


def _prepare_worker(
    request: Request,
    config: Config,
    *,
    worker_key: str,
    private_agent_names: list[str] | None,
) -> sandbox_worker_prep.PreparedWorkerRequest:
    runtime_paths = app_runtime_paths(request.app)
    normalized_worker_key = _normalized_worker_key(request, worker_key)
    try:
        prepared = sandbox_worker_prep.prepare_worker_request(
            worker_key=normalized_worker_key,
            base_dir=None,
            runtime_paths=runtime_paths,
            agent_policies=config.get_agent_policies(),
            private_agent_names=(frozenset(private_agent_names) if private_agent_names is not None else None),
            runner_token=app_runner_token(request.app),
        )
    except sandbox_worker_prep.WorkerRequestPreparationError as exc:
        status_code = 503 if exc.failure_kind == "worker" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return prepared


def _workspace_file(
    workspace: Path,
    relative_path: Path,
    *,
    label: str,
    byte_limit: int,
) -> Path:
    candidate = workspace / relative_path
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail=f"{label} must not be a symbolic link.")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} is unavailable in the worker workspace.") from exc
    try:
        resolved = resolve_path_within_root(workspace, resolved, symlinks="internal", strict=True)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{label} must be a regular file inside the worker workspace.",
        ) from None
    if not stat.S_ISREG(metadata.st_mode):
        raise HTTPException(status_code=400, detail=f"{label} must be a regular file inside the worker workspace.")
    if metadata.st_size <= 0 or metadata.st_size > byte_limit:
        raise HTTPException(status_code=400, detail=f"{label} exceeds its supported size.")
    return resolved


def _script_snapshot(workspace: Path, run_id: str) -> ScriptSnapshot:
    relative_root = launch_contract.snapshot_dir(run_id)
    return ScriptSnapshot(
        root=workspace,
        source=_workspace_file(
            workspace,
            relative_root / launch_contract.SOURCE_FILE_NAME,
            label="Script source",
            byte_limit=launch_contract.MAX_SOURCE_BYTES,
        ),
        capability=_workspace_file(
            workspace,
            relative_root / launch_contract.CAPABILITY_FILE_NAME,
            label="Script capability file",
            byte_limit=launch_contract.MAX_TOKEN_BYTES,
        ),
    )


def _script_execution_workspace(
    request: Request,
    config: Config,
    payload: SandboxScriptRunRequest,
    *,
    snapshot_workspace: Path,
) -> Path:
    state_scope_worker_key = payload.state_scope_worker_key
    if state_scope_worker_key is None:
        return snapshot_workspace
    try:
        expected_worker_key = script_worker_key_for_run(state_scope_worker_key, payload.run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Script state scope is invalid for this run.") from exc
    if not secrets.compare_digest(expected_worker_key, payload.worker_key):
        raise HTTPException(status_code=400, detail="Script state scope does not own this run worker.")

    try:
        return resolve_script_state_workspace(
            request.app,
            config,
            state_scope_worker_key=state_scope_worker_key,
            agent_name=state_scope_worker_key.rsplit(":", maxsplit=1)[-1],
            private_agent_names=frozenset(payload.private_agent_names or ()),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_gateway_url(gateway_url: str) -> None:
    parsed_url = urlsplit(gateway_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise HTTPException(status_code=400, detail="Script gateway environment must contain a safe HTTP(S) URL.")


def _validate_source_digest(source_path: Path, expected_digest: str) -> None:
    actual_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if not secrets.compare_digest(actual_digest, expected_digest):
        raise HTTPException(status_code=400, detail="Script source digest does not match the launch receipt.")


@router.post("/run", response_model=SandboxScriptRunResponse)
async def run_script_in_worker(request: Request, payload: SandboxScriptRunRequest) -> SandboxScriptRunResponse:
    """Launch one verified source snapshot under the existing shell supervisor."""
    if not background_script_supervision_supported():
        return SandboxScriptRunResponse(ok=False, error="Background scripts require Linux process-group containment.")
    config = request_runtime_config(request.app, payload.config_snapshot)
    prepared = _prepare_worker(
        request,
        config,
        worker_key=payload.worker_key,
        private_agent_names=payload.private_agent_names,
    )
    snapshot = _script_snapshot(prepared.paths.workspace.resolve(), payload.run_id)
    _validate_source_digest(snapshot.source, payload.source_digest)
    workspace = _script_execution_workspace(request, config, payload, snapshot_workspace=snapshot.root)
    _validate_gateway_url(payload.gateway_url)
    python_executable, base_environment, _cwd = sandbox_exec.resolve_subprocess_worker_context(prepared.paths)
    if python_executable is None or base_environment is None:
        return SandboxScriptRunResponse(ok=False, error="Worker Python runtime is unavailable.")
    execution_environment = sandbox_exec.request_execution_env("python", None, app_runtime_paths(request.app))
    try:
        sandbox_env_assembly.build_request_execution_env(
            request_workspace=workspace,
            prepared=prepared,
            execution_env=execution_environment,
        )
    except sandbox_exec.WorkspaceEnvHookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        socket_path = await asyncio.to_thread(ensure_shell_supervisor)
    except ShellSupervisorStartupError as exc:
        return SandboxScriptRunResponse(ok=False, error=str(exc))
    refusal = await launch_supervised_script(
        socket_path,
        namespace=worker_script_namespace(payload.worker_key, payload.run_id),
        run_id=payload.run_id,
        source_digest=payload.source_digest,
        gateway_url=payload.gateway_url,
        python_executable=python_executable,
        environment={**base_environment, **execution_environment},
        snapshot=snapshot,
        workspace=workspace,
        max_runtime_seconds=payload.max_runtime_seconds,
    )
    return SandboxScriptRunResponse(ok=refusal is None, error=refusal)


@router.post("/{run_id}/status", response_model=SandboxScriptStatusResponse)
async def status_script_in_worker(
    request: Request,
    run_id: ScriptRunId,
    payload: SandboxScriptStatusRequest,
) -> SandboxScriptStatusResponse:
    """Poll one namespaced supervisor handle without owning lifecycle state."""
    normalized_worker_key = _normalized_worker_key(request, payload.worker_key)
    try:
        socket_path = await asyncio.to_thread(ensure_shell_supervisor)
        status = await supervised_script_status(
            socket_path,
            namespace=worker_script_namespace(normalized_worker_key, run_id),
            run_id=run_id,
        )
    except (ShellSupervisorStartupError, ScriptWorkerError) as exc:
        return SandboxScriptStatusResponse(ok=False, state="unknown", error=str(exc))
    return SandboxScriptStatusResponse(ok=True, state=status.state, output=status.output, exit_code=status.exit_code)


@router.post("/{run_id}/cancel", response_model=SandboxScriptCancelResponse)
async def cancel_script_in_worker(
    request: Request,
    run_id: ScriptRunId,
    payload: SandboxScriptControlRequest,
) -> SandboxScriptCancelResponse:
    """Signal one namespaced supervisor handle without changing durable desired state."""
    normalized_worker_key = _normalized_worker_key(request, payload.worker_key)
    try:
        socket_path = await asyncio.to_thread(ensure_shell_supervisor)
        receipt = await signal_supervised_script(
            socket_path,
            namespace=worker_script_namespace(normalized_worker_key, run_id),
            run_id=run_id,
            force=payload.force,
        )
    except (ShellSupervisorStartupError, ScriptWorkerError) as exc:
        return SandboxScriptCancelResponse(ok=False, cancel_requested=False, error=str(exc))
    return SandboxScriptCancelResponse(
        ok=True,
        cancel_requested=receipt.cancel_requested,
        already_finished=receipt.already_finished,
        unknown_handle=receipt.unknown_handle,
    )
