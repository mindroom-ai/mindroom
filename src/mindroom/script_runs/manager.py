"""Primary-owned lifecycle management for background Python scripts."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from mindroom.constants import CONTROL_STATE_PATH_ENV
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.policy import resolve_script_launch_grants
from mindroom.script_runs.store import ScriptRunNotFoundError, ScriptRunStore, mint_script_capability
from mindroom.script_runs.worker_client import ScriptWorkerClient, WorkerScriptStatus
from mindroom.shell_supervisor import (
    check_command_via_supervisor,
    ensure_shell_supervisor,
    kill_command_via_supervisor,
    run_command_via_supervisor,
)
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config
from mindroom.tool_system.worker_routing import (
    agent_workspace_root_path,
    build_agent_toolkit_worker_target,
    serialize_tool_execution_identity,
    worker_root_path,
)
from mindroom.workers.models import WorkerHandle, WorkerSpec
from mindroom.workspaces import resolve_workspace_relative_path

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workers.backend import WorkerBackend

__all__ = ["ScriptRunLimits", "ScriptRunManager", "ScriptRunManagerError", "ScriptRunStatus"]

_MAX_SOURCE_BYTES = 128 * 1024
_LOCAL_EXECUTION_MODES = frozenset({"off", "local", "disabled"})
_WORKER_EXECUTION_MODES = frozenset({"all", "sandbox_all", "selective", "sandbox_selective"})
_HANDLE_RE = re.compile(r"shell:[0-9a-f]{8}")
_FINISHED_RE = re.compile(r"Status: FINISHED \(exit code (-?\d+)\)")
_TERMINAL_STATES = frozenset(
    {
        ScriptRunState.EXITED,
        ScriptRunState.FAILED,
        ScriptRunState.CANCELLED,
        ScriptRunState.INTERRUPTED,
    },
)


class ScriptRunManagerError(ValueError):
    """Raised when a background script lifecycle request cannot be fulfilled."""


class _ScriptBroker(Protocol):
    async def cancel_run(self, run_id: str) -> None:
        """Cancel in-process broker executions for one revoked run."""


@dataclass(frozen=True, slots=True)
class ScriptRunLimits:
    """Per-tool limits captured durably when a script starts."""

    allowed_tools: tuple[str, ...] | None = None
    max_concurrent_runs: int = 3
    max_tool_calls_per_minute: int = 30
    max_runtime_hours: float = 24

    def __post_init__(self) -> None:
        """Reject limits that cannot be enforced safely and predictably."""
        if (
            isinstance(self.max_concurrent_runs, bool)
            or not isinstance(self.max_concurrent_runs, int)
            or isinstance(self.max_tool_calls_per_minute, bool)
            or not isinstance(self.max_tool_calls_per_minute, int)
            or self.max_concurrent_runs <= 0
            or self.max_tool_calls_per_minute <= 0
        ):
            msg = "Background script limits must be positive."
            raise ScriptRunManagerError(msg)
        if (
            isinstance(self.max_runtime_hours, bool)
            or not isinstance(self.max_runtime_hours, int | float)
            or not math.isfinite(self.max_runtime_hours)
            or self.max_runtime_hours <= 0
        ):
            msg = "Background script runtime limit must be positive and finite."
            raise ScriptRunManagerError(msg)
        if self.allowed_tools is not None and any(not name.strip() for name in self.allowed_tools):
            msg = "Background script allowed tools must contain non-empty names."
            raise ScriptRunManagerError(msg)


@dataclass(frozen=True, slots=True)
class ScriptRunStatus:  # privata: ignore -- Task 6 lifecycle consumes this status contract.
    """One durable run record paired with its latest supervisor output."""

    run: ScriptRunRecord
    output: str = ""


@dataclass(slots=True)
class ScriptRunManager:
    """Own durable script intent while existing supervisors own process signals."""

    store: ScriptRunStore
    broker: _ScriptBroker
    worker_client: ScriptWorkerClient
    worker_backend: WorkerBackend | None
    gateway_url: str
    grant_resolver: Callable[[ToolRuntimeContext], tuple[ScriptToolGrant, ...]] = resolve_script_launch_grants
    _launch_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def run(
        self,
        context: ToolRuntimeContext,
        *,
        source: str | None = None,
        path: str | None = None,
        name: str | None = None,
        limits: ScriptRunLimits | None = None,
    ) -> ScriptRunRecord:
        """Snapshot and launch one Python source under its resolved execution scope."""
        effective_limits = limits or ScriptRunLimits()
        source_bytes = self._resolve_source(context, source=source, path=path)
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        execution_identity = build_execution_identity_from_runtime_context(context)
        worker_target = build_agent_toolkit_worker_target(
            context.config.resolve_entity(context.agent_name).execution_scope,
            context.agent_name,
            is_private=context.config.get_agent(context.agent_name).private is not None,
            execution_identity=execution_identity,
            runtime_paths=context.runtime_paths,
        )
        execution_mode = sandbox_proxy_config(context.runtime_paths).execution_mode
        if execution_mode in _WORKER_EXECUTION_MODES or (execution_mode is None and self.worker_backend is not None):
            if worker_target.worker_key is None:
                msg = "Background script worker scope could not be resolved for this requester."
                raise ScriptRunManagerError(msg)
            worker_key = worker_target.worker_key
            local_unsafe = False
        elif execution_mode in _LOCAL_EXECUTION_MODES:
            worker_key = None
            local_unsafe = True
        else:
            msg = "Background scripts require a worker or an explicitly disabled sandbox."
            raise ScriptRunManagerError(msg)

        token, token_hash = mint_script_capability()
        run_id = f"script-{uuid.uuid4().hex}"
        launch_grants = self.grant_resolver(context)
        if effective_limits.allowed_tools:
            allowed_tools = frozenset(effective_limits.allowed_tools)
            launch_grants = tuple(grant for grant in launch_grants if grant.toolkit_name in allowed_tools)
        run = ScriptRunRecord(
            run_id=run_id,
            agent_name=context.agent_name,
            owner_user_id=context.requester_id,
            room_id=context.room_id,
            thread_root_event_id=context.resolved_thread_id,
            execution_identity=serialize_tool_execution_identity(execution_identity),
            source_digest=source_digest,
            grants=launch_grants,
            token_hash=token_hash,
            worker_key=worker_key,
            name=_validated_name(name),
            local_unsafe=local_unsafe,
            max_tool_calls_per_minute=effective_limits.max_tool_calls_per_minute,
            max_runtime_seconds=max(1, round(effective_limits.max_runtime_hours * 60 * 60)),
        )

        async with self._launch_lock:
            active = await asyncio.to_thread(
                self.store.list_runs,
                agent_name=context.agent_name,
                owner_user_id=context.requester_id,
                include_finished=False,
            )
            scoped_active = [candidate for candidate in active if candidate.worker_key == worker_key]
            if len(scoped_active) >= effective_limits.max_concurrent_runs:
                msg = "Background script concurrent-run limit exceeded."
                raise ScriptRunManagerError(msg)
            await asyncio.to_thread(self.store.create_run, run)
            try:
                if local_unsafe:
                    return await self._launch_local(context, run=run, source=source_bytes, token=token)
                return await self._launch_worker(
                    context,
                    run=run,
                    source=source_bytes,
                    token=token,
                    worker_spec=WorkerSpec(
                        worker_key=_require_worker_key(worker_key),
                        private_agent_names=worker_target.private_agent_names,
                    ),
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    failure_state = ScriptRunState.INTERRUPTED
                else:
                    failure_state = ScriptRunState.FAILED
                await asyncio.to_thread(
                    self.store.transition_run,
                    run.run_id,
                    state=failure_state,
                    error=_bounded_error(exc),
                )
                try:
                    await self.broker.cancel_run(run.run_id)
                finally:
                    self._cleanup_token(context, run)
                raise

    async def status(
        self,
        context: ToolRuntimeContext,
        *,
        run_id: str,
    ) -> ScriptRunStatus:
        """Return one owned run after reconciling its supervisor state."""
        run = self._owned_run(context, run_id)
        if run.state in _TERMINAL_STATES:
            try:
                await self.broker.cancel_run(run.run_id)
            finally:
                self._cleanup_token(context, run)
            return ScriptRunStatus(run=run)
        if _runtime_expired(run):
            reconciled = await self.reconcile(context, run_id=run.run_id)
            return ScriptRunStatus(run=reconciled)
        status = await self._process_status(run)
        reconciled = await self._apply_process_status(context, run, status)
        return ScriptRunStatus(run=reconciled, output=status.output)

    async def cancel(
        self,
        context: ToolRuntimeContext,
        *,
        run_id: str,
        force: bool = False,
        reason: str = "Cancellation requested by the owning agent.",
    ) -> ScriptRunRecord:
        """Revoke one run durably before signalling its existing supervisor."""
        run = self._owned_run(context, run_id)
        if run.state in _TERMINAL_STATES:
            try:
                await self.broker.cancel_run(run.run_id)
            finally:
                self._cleanup_token(context, run)
            return run
        revoked = await asyncio.to_thread(self.store.request_cancel, run_id, reason=reason)
        try:
            await self.broker.cancel_run(run_id)
            if revoked.supervisor_handle is not None:
                await self._signal_process(revoked, force=force)
            terminal = await asyncio.to_thread(
                self.store.transition_run,
                run_id,
                state=ScriptRunState.CANCELLED,
            )
        except BaseException as exc:
            terminal = await asyncio.to_thread(
                self.store.transition_run,
                run_id,
                state=ScriptRunState.INTERRUPTED,
                error=_bounded_error(exc),
            )
            raise
        finally:
            self._cleanup_token(context, revoked)
        return terminal

    async def list(
        self,
        context: ToolRuntimeContext,
        *,
        include_finished: bool = True,
    ) -> builtins.list[ScriptRunRecord]:
        """List only runs owned by the current requester and agent."""
        return await asyncio.to_thread(
            self.store.list_runs,
            agent_name=context.agent_name,
            owner_user_id=context.requester_id,
            include_finished=include_finished,
        )

    async def reconcile(
        self,
        context: ToolRuntimeContext,
        *,
        run_id: str,
    ) -> ScriptRunRecord:
        """Reconcile one owned durable run with its current process fact."""
        run = self._owned_run(context, run_id)
        if run.state in _TERMINAL_STATES:
            try:
                await self.broker.cancel_run(run.run_id)
            finally:
                self._cleanup_token(context, run)
            return run
        if _runtime_expired(run):
            return await self.cancel(
                context,
                run_id=run.run_id,
                reason="Background script maximum runtime exceeded.",
            )
        status = await self._process_status(run)
        return await self._apply_process_status(context, run, status)

    async def _launch_worker(
        self,
        context: ToolRuntimeContext,
        *,
        run: ScriptRunRecord,
        source: bytes,
        token: str,
        worker_spec: WorkerSpec,
    ) -> ScriptRunRecord:
        if self.worker_backend is None:
            msg = "Background script worker backend is unavailable."
            raise ScriptRunManagerError(msg)
        worker = await asyncio.to_thread(self.worker_backend.ensure_worker, worker_spec)
        await asyncio.to_thread(
            self.store.transition_run,
            run.run_id,
            state=ScriptRunState.STARTING,
            worker_id=worker.worker_id,
        )
        workspace = _worker_workspace(context, worker)
        source_path, token_path = _write_snapshot(workspace, run.run_id, source=source, token=token)
        receipt = await self.worker_client.launch(
            worker,
            run_id=run.run_id,
            source_path=str(source_path.relative_to(workspace)),
            source_digest=run.source_digest,
            token_path=str(token_path.relative_to(workspace)),
            gateway_url=self.gateway_url,
            private_agent_names=(
                tuple(sorted(worker_spec.private_agent_names)) if worker_spec.private_agent_names is not None else None
            ),
        )
        try:
            return await asyncio.to_thread(
                self.store.transition_run,
                run.run_id,
                state=ScriptRunState.RUNNING,
                worker_id=worker.worker_id,
                supervisor_handle=receipt.supervisor_handle,
            )
        except BaseException:
            with suppress(Exception):
                await self.worker_client.cancel(
                    worker,
                    run_id=run.run_id,
                    supervisor_handle=receipt.supervisor_handle,
                    force=True,
                )
            raise

    async def _launch_local(
        self,
        context: ToolRuntimeContext,
        *,
        run: ScriptRunRecord,
        source: bytes,
        token: str,
    ) -> ScriptRunRecord:
        workspace = _agent_workspace(context)
        source_path, token_path = _write_snapshot(workspace, run.run_id, source=source, token=token)
        socket_path = ensure_shell_supervisor()
        environment = dict(os.environ)
        environment.update(context.runtime_paths.process_env)
        environment.pop(CONTROL_STATE_PATH_ENV, None)
        environment.update(
            {
                "MINDROOM_SCRIPT_GATEWAY_URL": self.gateway_url.rstrip("/"),
                "MINDROOM_SCRIPT_RUN_ID": run.run_id,
                "MINDROOM_SCRIPT_SOURCE_DIGEST": run.source_digest,
                "MINDROOM_SCRIPT_TOKEN_PATH": str(token_path),
                "MINDROOM_SCRIPT_WORKSPACE_ROOT": str(workspace),
            },
        )
        message = await run_command_via_supervisor(
            socket_path,
            namespace=_local_namespace(run.run_id),
            argv=[sys.executable, "-m", "mindroom.script_runs.shim", str(source_path), str(token_path)],
            env=environment,
            cwd=str(workspace),
            tail=200,
            timeout=0,
        )
        match = _HANDLE_RE.search(message)
        if match is None:
            raise ScriptRunManagerError(message)
        supervisor_handle = match.group(0)
        try:
            return await asyncio.to_thread(
                self.store.transition_run,
                run.run_id,
                state=ScriptRunState.RUNNING,
                supervisor_handle=supervisor_handle,
            )
        except BaseException:
            with suppress(Exception):
                await asyncio.to_thread(
                    kill_command_via_supervisor,
                    socket_path,
                    namespace=_local_namespace(run.run_id),
                    handle=supervisor_handle,
                    force=True,
                )
            raise

    def _owned_run(self, context: ToolRuntimeContext, run_id: str) -> ScriptRunRecord:
        not_found = f"Background script '{run_id}' was not found."
        try:
            run = self.store.get_run(run_id)
        except ScriptRunNotFoundError:
            raise ScriptRunManagerError(not_found) from None
        if run.agent_name != context.agent_name or run.owner_user_id != context.requester_id:
            raise ScriptRunManagerError(not_found)
        return run

    def _resolve_source(self, context: ToolRuntimeContext, *, source: str | None, path: str | None) -> bytes:
        if (source is None) == (path is None):
            msg = "Provide exactly one of source or path."
            raise ScriptRunManagerError(msg)
        if source is not None:
            source_bytes = source.encode("utf-8")
        else:
            workspace = _agent_workspace(context)
            try:
                source_path = resolve_workspace_relative_path(
                    workspace,
                    path or "",
                    field_name="Script source path",
                )
                if source_path.is_symlink() or not source_path.is_file():
                    msg = "Script source path must be a regular file in the agent workspace."
                    raise ScriptRunManagerError(msg)
                with source_path.open("rb") as source_file:
                    source_bytes = source_file.read(_MAX_SOURCE_BYTES + 1)
            except (OSError, ValueError) as exc:
                raise ScriptRunManagerError(str(exc)) from exc
        if not source_bytes:
            msg = "Background script source must not be empty."
            raise ScriptRunManagerError(msg)
        if len(source_bytes) > _MAX_SOURCE_BYTES:
            msg = f"Background script source exceeds the {_MAX_SOURCE_BYTES}-byte limit."
            raise ScriptRunManagerError(msg)
        return source_bytes

    async def _process_status(self, run: ScriptRunRecord) -> WorkerScriptStatus:
        if run.supervisor_handle is None:
            return WorkerScriptStatus.unknown_handle()
        if run.local_unsafe:
            message = await asyncio.to_thread(
                check_command_via_supervisor,
                ensure_shell_supervisor(),
                namespace=_local_namespace(run.run_id),
                handle=run.supervisor_handle,
            )
            return _parse_local_status(message)
        worker = self._worker_handle(run)
        if worker is None:
            return WorkerScriptStatus.unknown_handle()
        return await self.worker_client.status(
            worker,
            run_id=run.run_id,
            supervisor_handle=run.supervisor_handle,
        )

    async def _apply_process_status(
        self,
        context: ToolRuntimeContext,
        run: ScriptRunRecord,
        status: WorkerScriptStatus,
    ) -> ScriptRunRecord:
        if status.state == "running":
            if run.worker_key is not None and self.worker_backend is not None:
                await asyncio.to_thread(self.worker_backend.touch_worker, run.worker_key)
            return run
        if status.state == "unknown":
            state = ScriptRunState.INTERRUPTED
            error = "Background script supervisor handle is unavailable."
        elif status.exit_code == 0:
            state = ScriptRunState.EXITED
            error = None
        else:
            state = ScriptRunState.FAILED
            error = status.output or f"Background script exited with code {status.exit_code}."
        terminal = await asyncio.to_thread(
            self.store.transition_run,
            run.run_id,
            state=state,
            exit_code=status.exit_code,
            error=error,
        )
        try:
            await self.broker.cancel_run(run.run_id)
        finally:
            self._cleanup_token(context, terminal)
        return terminal

    async def _signal_process(self, run: ScriptRunRecord, *, force: bool) -> None:
        if run.supervisor_handle is None:
            return
        if run.local_unsafe:
            await asyncio.to_thread(
                kill_command_via_supervisor,
                ensure_shell_supervisor(),
                namespace=_local_namespace(run.run_id),
                handle=run.supervisor_handle,
                force=force,
            )
            return
        worker = self._worker_handle(run)
        if worker is None:
            return
        await self.worker_client.cancel(
            worker,
            run_id=run.run_id,
            supervisor_handle=run.supervisor_handle,
            force=force,
        )

    def _worker_handle(self, run: ScriptRunRecord) -> WorkerHandle | None:
        if self.worker_backend is None or run.worker_id is None or run.worker_key is None:
            return None
        workers = self.worker_backend.list_workers(include_idle=True)
        return next(
            (worker for worker in workers if worker.worker_id == run.worker_id and worker.worker_key == run.worker_key),
            None,
        )

    def _cleanup_token(self, context: ToolRuntimeContext, run: ScriptRunRecord) -> None:
        workspace: Path | None
        if run.local_unsafe:
            workspace = _agent_workspace(context)
        else:
            worker = self._worker_handle(run)
            workspace = (
                _worker_workspace(context, worker) if worker is not None else _worker_workspace_from_run(context, run)
            )
        if workspace is None:
            return
        _remove_snapshot_token(workspace, run.run_id)


def _agent_workspace(context: ToolRuntimeContext) -> Path:
    execution_identity = build_execution_identity_from_runtime_context(context)
    runtime = resolve_agent_runtime(
        context.agent_name,
        context.config,
        context.runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    workspace = (
        runtime.workspace.root
        if runtime.workspace is not None
        else agent_workspace_root_path(context.runtime_paths.storage_root, context.agent_name)
    )
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace.resolve()


def _worker_workspace(context: ToolRuntimeContext, worker: WorkerHandle) -> Path:
    state_root = worker.debug_metadata.get("state_root")
    if state_root is not None:
        root = Path(state_root)
    elif (state_subpath := worker.debug_metadata.get("state_subpath")) is not None:
        root = context.runtime_paths.storage_root / state_subpath
    else:
        root = worker_root_path(context.runtime_paths.storage_root, worker.worker_key)
    resolved_storage_root = context.runtime_paths.storage_root.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_relative_to(resolved_storage_root):
        msg = "Background script worker state root must stay inside primary storage."
        raise ScriptRunManagerError(msg)
    workspace = resolved_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    resolved_workspace = workspace.resolve()
    if not resolved_workspace.is_relative_to(resolved_root):
        msg = "Background script workspace must stay inside its worker state root."
        raise ScriptRunManagerError(msg)
    return resolved_workspace


def _worker_workspace_from_run(context: ToolRuntimeContext, run: ScriptRunRecord) -> Path | None:
    if run.worker_key is None:
        return None
    workspace = worker_root_path(context.runtime_paths.storage_root, run.worker_key) / "workspace"
    return workspace.resolve() if workspace.exists() else None


def _snapshot_relative_dir(run_id: str) -> Path:
    return Path(".mindroom") / "script-runs" / run_id


def _write_snapshot(workspace: Path, run_id: str, *, source: bytes, token: str) -> tuple[Path, Path]:
    run_dir = resolve_workspace_relative_path(
        workspace,
        _snapshot_relative_dir(run_id),
        field_name="Script run directory",
    )
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    run_dir.chmod(0o700)
    source_path = run_dir / "source.py"
    token_path = run_dir / "capability"
    _write_private_file(source_path, source)
    _write_private_file(token_path, token.encode("utf-8"))
    return source_path, token_path


def _write_private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _remove_snapshot_token(workspace: Path, run_id: str) -> None:
    token_path = workspace / _snapshot_relative_dir(run_id) / "capability"
    current = workspace
    for part in token_path.parent.relative_to(workspace).parts:
        current /= part
        if current.is_symlink():
            return
    token_path.unlink(missing_ok=True)


def _parse_local_status(message: str) -> WorkerScriptStatus:
    if message.startswith("Status: RUNNING"):
        return WorkerScriptStatus(state="running", output=message)
    finished = _FINISHED_RE.match(message)
    if finished is not None:
        return WorkerScriptStatus(state="exited", output=message, exit_code=int(finished.group(1)))
    return WorkerScriptStatus.unknown_handle()


def _local_namespace(run_id: str) -> str:
    return f"script:local:{run_id}"


def _runtime_expired(run: ScriptRunRecord) -> bool:
    started_at = run.started_at or run.created_at
    started = datetime.fromisoformat(started_at)
    return (datetime.now(UTC) - started).total_seconds() >= run.max_runtime_seconds


def _validated_name(name: str | None) -> str | None:
    if name is None:
        return None
    normalized = name.strip()
    if not normalized:
        return None
    if len(normalized) > 120:
        msg = "Background script name must be at most 120 characters."
        raise ScriptRunManagerError(msg)
    return normalized


def _bounded_error(exc: BaseException) -> str:
    value = str(exc) or exc.__class__.__name__
    return value.encode("utf-8")[: 64 * 1024].decode("utf-8", errors="ignore")


def _require_worker_key(worker_key: str | None) -> str:
    if worker_key is None:
        msg = "Background script worker scope is unavailable."
        raise ScriptRunManagerError(msg)
    return worker_key
