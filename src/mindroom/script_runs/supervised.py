"""Launch, poll, and signal one background script process through a shell supervisor.

Unsafe-local runs use the primary's supervisor and worker runs their worker's.
Each caller starts its own supervisor, because a supervisor that fails to start means something different to each.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.runtime_env_policy import CONTROL_STATE_PATH_ENV
from mindroom.script_runs import launch_contract
from mindroom.script_runs.models import supervisor_handle_for_run
from mindroom.script_runs.wire import ScriptWorkerError, WorkerScriptCancel, WorkerScriptStatus
from mindroom.shell.supervisor import (
    check_command_via_supervisor,
    kill_command_via_supervisor,
    parse_shell_supervisor_kill,
    parse_shell_supervisor_status,
    run_command_via_supervisor,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "ScriptSnapshot",
    "launch_supervised_script",
    "local_script_namespace",
    "signal_supervised_script",
    "supervised_script_status",
    "worker_script_namespace",
]


@dataclass(frozen=True, slots=True)
class ScriptSnapshot:
    """One run's launch snapshot: the root it was written under, its source, and its capability file."""

    root: Path
    source: Path
    capability: Path


def local_script_namespace(run_id: str) -> str:
    """Return the supervisor namespace of one unsafe-local run."""
    return f"script:local:{run_id}"


def worker_script_namespace(worker_key: str, run_id: str) -> str:
    """Return the supervisor namespace of one worker run, length-prefixed so no two key and run pairs collide."""
    return f"script:{len(worker_key)}:{worker_key}:{run_id}"


async def launch_supervised_script(
    socket_path: str,
    *,
    namespace: str,
    run_id: str,
    source_digest: str,
    gateway_url: str,
    python_executable: str,
    environment: Mapping[str, str],
    snapshot: ScriptSnapshot,
    workspace: Path,
    max_runtime_seconds: int,
) -> str | None:
    """Start the shim on one run's snapshot; return None once the supervisor owns its handle, else the refusal."""
    script_environment = {
        **environment,
        launch_contract.GATEWAY_URL_ENV: gateway_url.rstrip("/"),
        launch_contract.RUN_ID_ENV: run_id,
        launch_contract.SOURCE_DIGEST_ENV: source_digest,
        launch_contract.SNAPSHOT_ROOT_ENV: str(snapshot.root),
        launch_contract.TOKEN_PATH_ENV: str(snapshot.capability),
        launch_contract.WORKSPACE_ROOT_ENV: str(workspace),
    }
    script_environment.pop(CONTROL_STATE_PATH_ENV, None)
    handle = supervisor_handle_for_run(run_id)
    result = await run_command_via_supervisor(
        socket_path,
        namespace=namespace,
        # The shim runs from the workspace: `-P` and `-s` keep its cwd and the user site off `sys.path`.
        argv=[
            python_executable,
            "-P",
            "-s",
            "-m",
            "mindroom.script_runs.shim",
            str(snapshot.source),
            str(snapshot.capability),
        ],
        env=script_environment,
        cwd=str(workspace),
        tail=200,
        timeout=0,
        handle=handle,
        max_runtime_seconds=max_runtime_seconds,
    )
    return None if result.handle == handle else result.message


async def supervised_script_status(socket_path: str, *, namespace: str, run_id: str) -> WorkerScriptStatus:
    """Return one run's process state; raise `ScriptWorkerError` on a reply the supervisor does not vouch for."""
    message = await asyncio.to_thread(
        check_command_via_supervisor,
        socket_path,
        namespace=namespace,
        handle=supervisor_handle_for_run(run_id),
    )
    status = parse_shell_supervisor_status(message)
    if status.state == "error":
        raise ScriptWorkerError(message)
    if status.state == "unknown":
        return WorkerScriptStatus(state="unknown")
    return WorkerScriptStatus(state=status.state, output=message, exit_code=status.exit_code)


async def signal_supervised_script(
    socket_path: str,
    *,
    namespace: str,
    run_id: str,
    force: bool,
) -> WorkerScriptCancel:
    """Send SIGTERM, or SIGKILL with *force*, to one run's process group; raise `ScriptWorkerError` on any other reply."""
    message = await asyncio.to_thread(
        kill_command_via_supervisor,
        socket_path,
        namespace=namespace,
        handle=supervisor_handle_for_run(run_id),
        force=force,
    )
    outcome = parse_shell_supervisor_kill(message)
    if outcome == "error":
        raise ScriptWorkerError(message)
    return WorkerScriptCancel(
        cancel_requested=outcome == "signalled",
        already_finished=outcome == "finished",
        unknown_handle=outcome == "unknown",
    )
