"""Authenticated, single-turn CLI runtime installation and pinned shell transport."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from mindroom.agent_cli.shell_invocation import (
    InvalidShellArgumentsError,
    build_agent_cli_shell,
    invoke_agent_cli_shell,
)
from mindroom.agent_cli.worker_network import probe_cli_network
from mindroom.agent_cli.worker_protocol import CLI_PRIVATE_ROOT_PATH, CliShellRequest, CliWorkerLaunch
from mindroom.api import sandbox_env_assembly, sandbox_exec, sandbox_worker_prep
from mindroom.api.sandbox_runner import (
    app_cli_state,
    app_runner_token,
    app_runtime_paths,
    validate_runner_token,
)
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.shell_supervisor import ensure_shell_supervisor
from mindroom.tools.shell import AgentCliShellBinding
from mindroom.workers.models import is_cli_worker_key

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_CLI_PRIVATE_ROOT = Path(CLI_PRIVATE_ROOT_PATH)
router = APIRouter(prefix="/api/sandbox-runner/agent-cli", dependencies=[Depends(validate_runner_token)])


@dataclass
class CliWorkerRuntime:
    """One installed turn with its prepared worker request and reserved shell handles."""

    launch: CliWorkerLaunch
    token_path: Path
    socket_path: str
    prepared: sandbox_worker_prep.PreparedWorkerRequest
    handles: set[str] = field(default_factory=set)


def _require_worker(request: Request, worker_key: str) -> RuntimePaths:
    runtime = app_runtime_paths(request.app)
    if not is_cli_worker_key(worker_key) or sandbox_exec.runner_dedicated_worker_key(runtime) != worker_key:
        raise HTTPException(403, "CLI runtime requires its dedicated isolated worker")
    return runtime


def _workspace(launch: CliWorkerLaunch, runtime: RuntimePaths) -> Path:
    # CLI workers start without agent config, so they cannot resolve worker scopes or private
    # roots. The primary resolves the canonical workspace from its live policies, refuses any
    # other path, and maps it onto the mounts it planned for this worker (CliWorkerLease).
    workspace = Path(launch.shell.workspace).resolve()
    storage_root = sandbox_exec.runner_storage_root(runtime)
    if not workspace.is_relative_to(storage_root):
        raise HTTPException(400, "CLI workspace is outside the worker's storage mount")
    if _CLI_PRIVATE_ROOT.resolve().is_relative_to(runtime.storage_root.resolve()):
        raise HTTPException(503, "CLI capability storage overlaps worker state")
    return workspace


@router.post("/install")
async def install_cli_runtime(payload: CliWorkerLaunch, request: Request) -> dict[str, bool]:
    """Install the grant once, only after independent control auth and network probes."""
    runtime = _require_worker(request, payload.worker_key)
    cli_state = app_cli_state(request.app)
    if cli_state.install_started:
        raise HTTPException(409, "CLI worker was already assigned a turn")
    workspace = _workspace(payload, runtime)
    # Fence concurrent installs before any await. A failed worker must be retired,
    # never revived with another generation's grant or surviving shell process.
    cli_state.install_started = True
    token = app_runner_token(request.app)
    assert token is not None
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            try:
                await probe_cli_network(payload, control_token=token, client=client)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                # Distinct from failed isolation: the primary can name its address and the fix.
                raise HTTPException(504, "CLI worker cannot reach its primary") from exc
        prepared = await run_blocking_until_complete(
            partial(
                sandbox_worker_prep.prepare_worker_request,
                worker_key=payload.worker_key,
                tool_init_overrides={"base_dir": str(workspace)},
                runtime_paths=runtime,
                # No agent config here; this dedicated worker's root bounds base_dir.
                agent_policies={},
                private_agent_names=frozenset(payload.private_agent_names),
                runner_token=token,
            ),
        )
        workspace.mkdir(parents=True, exist_ok=True)
        _CLI_PRIVATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=False)
        token_path = _CLI_PRIVATE_ROOT / "capability"
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(payload.token.get_secret_value())
        socket_path = ensure_shell_supervisor()
    except (ValueError, OSError, httpx.HTTPError) as exc:
        raise HTTPException(503, "CLI worker isolation or private runtime setup failed") from exc
    cli_state.runtime = CliWorkerRuntime(payload, token_path, socket_path, prepared)
    return {"ok": True}


def _shell_runtime(state: CliWorkerRuntime, runtime: RuntimePaths) -> RuntimePaths:
    # Rebuilt per request like the canonical sandbox runner, so workspace env hooks stay current.
    execution_env = sandbox_exec.worker_subprocess_env(state.prepared.paths)
    env_result = sandbox_env_assembly.build_request_execution_env(
        request_workspace=Path(state.launch.shell.workspace),
        prepared=state.prepared,
        execution_env=execution_env,
    )
    return sandbox_exec.tool_runtime_paths_with_request_env(
        runtime,
        execution_env,
        trusted_env_overlay=env_result.trusted_overlay,
    )


@router.post("/shell")
async def invoke_cli_shell(payload: CliShellRequest, request: Request) -> dict[str, object]:
    """Invoke canonical shell behavior with an owner-reserved supervisor handle."""
    runtime = _require_worker(request, payload.worker_key)
    state = app_cli_state(request.app).runtime
    if state is None:
        raise HTTPException(409, "CLI grant is not installed")
    operation = payload.operation
    if operation.function_name == "run_shell_command":
        if payload.handle in state.handles:
            raise HTTPException(409, "CLI shell handle was already reserved")
        state.handles.add(payload.handle)
    elif payload.handle not in state.handles:
        raise HTTPException(403, "CLI shell handle does not belong to this turn")
    launch = state.launch
    binding = AgentCliShellBinding(
        state.socket_path,
        f"agent-cli:{launch.worker_key}:{launch.turn_id}:{launch.generation}",
        payload.handle,
        launch.primary_url,
        str(state.token_path),
    )
    shell_runtime = await run_blocking_until_complete(_shell_runtime, state, runtime)
    toolkit = build_agent_cli_shell(launch.shell, runtime_paths=shell_runtime, binding=binding)
    arguments = operation.model_dump(exclude={"function_name"}, exclude_none=True)
    if operation.function_name != "run_shell_command":
        arguments["handle"] = payload.handle
    elif "handle" in arguments:
        raise HTTPException(422, "Canonical shell run cannot select its supervisor handle")
    try:
        result = await invoke_agent_cli_shell(toolkit, operation.function_name, arguments)
    except InvalidShellArgumentsError as exc:
        raise HTTPException(422, "Invalid canonical shell arguments") from exc
    return {"result": result}
