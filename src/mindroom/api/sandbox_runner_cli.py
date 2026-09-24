"""Authenticated, single-turn CLI runtime installation and pinned shell transport."""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from mindroom.agent_cli.worker_network import probe_cli_network
from mindroom.agent_cli.worker_protocol import CLI_PRIVATE_ROOT_PATH, CliShellRequest, CliWorkerLaunch
from mindroom.api import sandbox_env_assembly, sandbox_exec, sandbox_worker_prep
from mindroom.api.sandbox_runner import (
    app_cli_state,
    app_runner_token,
    app_runtime_paths,
    app_user_scope_agent_names,
    validate_runner_token,
)
from mindroom.background_tasks import run_blocking_until_complete, wait_for_future_until_complete
from mindroom.shell_supervisor import ensure_shell_supervisor
from mindroom.tool_system.output_files import ToolOutputFilePolicy, wrap_toolkit_for_output_files
from mindroom.tool_system.tool_access import function_schema, validate_tool_arguments
from mindroom.tool_system.worker_routing import visible_state_roots_for_worker_key
from mindroom.tools.shell import ShellWorkerBinding, shell_tools
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


def _workspace(launch: CliWorkerLaunch, runtime: RuntimePaths, user_scope_agent_names: frozenset[str]) -> Path:
    roots = visible_state_roots_for_worker_key(
        runtime.storage_root,
        launch.state_scope_worker_key,
        private_agent_names=frozenset(launch.private_agent_names),
        user_scope_agent_names=user_scope_agent_names,
    )
    workspace = Path(launch.shell.workspace).resolve()
    if not any(workspace.is_relative_to(root.resolve()) for root in roots):
        raise HTTPException(400, "CLI workspace is outside its canonical state mount")
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
    user_scope_agent_names = app_user_scope_agent_names(request.app)
    workspace = _workspace(payload, runtime, user_scope_agent_names)
    # Fence concurrent installs before any await. A failed worker must be retired,
    # never revived with another generation's grant or surviving shell process.
    cli_state.install_started = True
    token = app_runner_token(request.app)
    assert token is not None
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False) as client:
            await probe_cli_network(payload, control_token=token, client=client)
        prepared = await run_blocking_until_complete(
            partial(
                sandbox_worker_prep.prepare_worker_request,
                worker_key=payload.worker_key,
                tool_init_overrides={"base_dir": str(workspace)},
                runtime_paths=runtime,
                private_agent_names=frozenset(payload.private_agent_names),
                user_scope_agent_names=user_scope_agent_names,
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
    workspace = Path(state.launch.shell.workspace)
    env_result = sandbox_env_assembly.build_request_execution_env(
        request_workspace=workspace,
        prepared=state.prepared,
        execution_env=execution_env,
        apply_workspace_env_hook=sandbox_worker_prep.workspace_env_hook_allowed(
            workspace,
            requester_bound=sandbox_worker_prep.requester_bound_runtime(state.launch.worker_key),
            state_worker_key=state.launch.state_scope_worker_key,
            prepared=state.prepared,
            runtime_paths=runtime,
        ),
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
    binding = ShellWorkerBinding(
        state.socket_path,
        f"agent-cli:{launch.worker_key}:{launch.turn_id}:{launch.generation}",
        payload.handle,
        launch.gateway_url,
        str(state.token_path),
    )
    shell_runtime = await run_blocking_until_complete(_shell_runtime, state, runtime)
    toolkit = shell_tools()(
        base_dir=launch.shell.workspace,
        shell_path_prepend=launch.shell.shell_path_prepend,
        runtime_paths=shell_runtime,
        worker_binding=binding,
    )
    wrap_toolkit_for_output_files(
        toolkit,
        ToolOutputFilePolicy(
            Path(launch.shell.workspace),
            max_bytes=launch.shell.output_max_bytes,
            auto_save_threshold_bytes=launch.shell.output_auto_save_threshold_bytes,
        ),
    )
    function = toolkit.async_functions.get(operation.function_name) or toolkit.functions[operation.function_name]
    entrypoint = function.entrypoint
    assert entrypoint is not None
    arguments = operation.model_dump(exclude={"function_name"}, exclude_none=True)
    if operation.function_name != "run_shell_command":
        arguments["handle"] = payload.handle
    elif "handle" in arguments:
        raise HTTPException(422, "Canonical shell run cannot select its supervisor handle")
    function.process_entrypoint()
    try:
        validate_tool_arguments(function_schema(function), arguments)
    except ValueError as exc:
        raise HTTPException(422, "Invalid canonical shell arguments") from exc
    # Sync supervisor calls must finish before the HTTP operation's lifetime ends.
    if inspect.iscoroutinefunction(entrypoint):
        result = await entrypoint(**arguments)
    else:
        task = asyncio.create_task(asyncio.to_thread(entrypoint, **arguments))
        result = await wait_for_future_until_complete(task)
    return {"result": result}
