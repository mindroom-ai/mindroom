"""Primary-owned lifecycle management for background Python scripts."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import stat
import sys
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from mindroom.constants import CONTROL_STATE_PATH_ENV
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.policy import resolve_script_launch_grants
from mindroom.script_runs.store import ScriptRunNotFoundError, ScriptRunStore, mint_script_capability
from mindroom.script_runs.worker_client import ScriptWorkerClient, WorkerScriptCancel, WorkerScriptStatus
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

logger = get_logger(__name__)

_MAX_SOURCE_BYTES = 128 * 1024
_LOCAL_EXECUTION_MODES = frozenset({"off", "local", "disabled"})
_WORKER_EXECUTION_MODES = frozenset({"all", "sandbox_all", "selective", "sandbox_selective"})
_HANDLE_RE = re.compile(r"shell:[0-9a-f]{32}")
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


class _AmbiguousLaunchError(Exception):
    """Carry the original launch error past generic pre-spawn failure handling."""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(str(cause))


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
    cancellation_grace_seconds: float = 2.0
    cancellation_poll_interval_seconds: float = 0.05
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
            "user_agent",
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
        supervisor_handle = f"shell:{uuid.uuid4().hex}"
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
            supervisor_handle=supervisor_handle,
            name=_validated_name(name),
            local_unsafe=local_unsafe,
            max_tool_calls_per_minute=effective_limits.max_tool_calls_per_minute,
            max_runtime_seconds=max(1, round(effective_limits.max_runtime_hours * 60 * 60)),
        )
        worker_spec = (
            None
            if local_unsafe
            else WorkerSpec(
                worker_key=_require_worker_key(worker_key),
                private_agent_names=worker_target.private_agent_names,
            )
        )
        return await self._create_and_launch(
            context,
            run=run,
            source=source_bytes,
            token=token,
            max_concurrent_runs=effective_limits.max_concurrent_runs,
            worker_spec=worker_spec,
        )

    async def _create_and_launch(
        self,
        context: ToolRuntimeContext,
        *,
        run: ScriptRunRecord,
        source: bytes,
        token: str,
        max_concurrent_runs: int,
        worker_spec: WorkerSpec | None,
    ) -> ScriptRunRecord:
        async with self._launch_lock:
            active = await asyncio.to_thread(
                self.store.list_runs,
                agent_name=context.agent_name,
                owner_user_id=context.requester_id,
                include_finished=False,
            )
            scoped_active = [candidate for candidate in active if candidate.worker_key == run.worker_key]
            if len(scoped_active) >= max_concurrent_runs:
                msg = "Background script concurrent-run limit exceeded."
                raise ScriptRunManagerError(msg)
            await asyncio.to_thread(self.store.create_run, run)
            try:
                created = await asyncio.to_thread(self.store.get_run, run.run_id)
                if created.cancel_requested_at is not None:
                    return await self._complete_cancel_before_spawn(context, created)
                if run.local_unsafe:
                    return await self._launch_local(context, run=run, source=source, token=token)
                return await self._launch_worker(
                    context,
                    run=run,
                    source=source,
                    token=token,
                    worker_spec=_require_worker_spec(worker_spec),
                )
            except _AmbiguousLaunchError as exc:
                await self._cleanup_token(context, run)
                raise exc.cause from None
            except BaseException as exc:
                durable: ScriptRunRecord | None = None
                with suppress(Exception):
                    durable = await asyncio.to_thread(self.store.get_run, run.run_id)
                if durable is not None and durable.cancel_requested_at is not None:
                    await self._cleanup_token(context, durable)
                    raise
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
                    await self._cleanup_token(context, run)
                raise

    async def _complete_cancel_before_spawn(
        self,
        context: ToolRuntimeContext,
        run: ScriptRunRecord,
    ) -> ScriptRunRecord:
        try:
            await self.broker.cancel_run(run.run_id)
        finally:
            await self._cleanup_token(context, run)
        return await asyncio.to_thread(
            self.store.transition_run,
            run.run_id,
            state=ScriptRunState.CANCELLED,
        )

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
                await self._cleanup_token(context, run)
            return ScriptRunStatus(run=run)
        if run.cancel_requested_at is not None:
            reconciled = await self.cancel(
                context,
                run_id=run.run_id,
                reason=run.cancellation_reason or "Cancellation requested by the owning agent.",
            )
            return ScriptRunStatus(run=reconciled)
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
                await self._cleanup_token(context, run)
            return run
        revoked = await asyncio.to_thread(self.store.request_cancel, run_id, reason=reason)
        broker_error: BaseException | None = None
        process_error: BaseException | None = None
        process_status: WorkerScriptStatus | None = None
        try:
            await self.broker.cancel_run(run_id)
        except BaseException as exc:
            broker_error = exc
        try:
            process_status = await self._terminate_and_confirm(revoked, force=force)
        except BaseException as exc:
            process_error = exc
        finally:
            await self._cleanup_token(context, revoked)
        if broker_error is not None:
            raise broker_error
        if process_error is not None:
            raise process_error
        if process_status is None or process_status.state != "exited":
            msg = "Background script termination is not yet confirmed; retry cancellation."
            raise ScriptRunManagerError(msg)
        return await asyncio.to_thread(
            self.store.transition_run,
            run_id,
            state=ScriptRunState.CANCELLED,
            exit_code=process_status.exit_code,
        )

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
                await self._cleanup_token(context, run)
            return run
        if run.cancel_requested_at is not None:
            return await self.cancel(
                context,
                run_id=run.run_id,
                reason=run.cancellation_reason or "Cancellation requested by the owning agent.",
            )
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
        assigned = await asyncio.to_thread(self.store.get_run, run.run_id)
        if assigned.cancel_requested_at is not None:
            return await self._complete_cancel_before_spawn(context, assigned)
        workspace = _worker_workspace(context, worker)
        source_path, token_path = _write_snapshot(workspace, run.run_id, source=source, token=token)
        ready = await asyncio.to_thread(self.store.get_run, run.run_id)
        if ready.cancel_requested_at is not None:
            return await self._complete_cancel_before_spawn(context, ready)
        supervisor_handle = _require_supervisor_handle(run.supervisor_handle)
        try:
            receipt = await self.worker_client.launch(
                worker,
                run_id=run.run_id,
                source_path=str(source_path.relative_to(workspace)),
                source_digest=run.source_digest,
                token_path=str(token_path.relative_to(workspace)),
                gateway_url=self.gateway_url,
                supervisor_handle=supervisor_handle,
                private_agent_names=(
                    tuple(sorted(worker_spec.private_agent_names))
                    if worker_spec.private_agent_names is not None
                    else None
                ),
            )
        except BaseException as exc:
            await self._preserve_ambiguous_launch(context, run.run_id)
            raise _AmbiguousLaunchError(exc) from exc
        launched = await asyncio.to_thread(self.store.get_run, run.run_id)
        if launched.cancel_requested_at is not None:
            await self._preserve_ambiguous_launch(context, run.run_id)
            return await asyncio.to_thread(self.store.get_run, run.run_id)
        try:
            return await asyncio.to_thread(
                self.store.transition_run,
                run.run_id,
                state=ScriptRunState.RUNNING,
                worker_id=worker.worker_id,
                supervisor_handle=receipt.supervisor_handle,
            )
        except BaseException as exc:
            durable: ScriptRunRecord | None = None
            with suppress(Exception):
                durable = await asyncio.to_thread(self.store.get_run, run.run_id)
            if durable is not None and durable.cancel_requested_at is not None:
                if durable.state not in _TERMINAL_STATES:
                    await self._preserve_ambiguous_launch(context, run.run_id)
                    durable = await asyncio.to_thread(self.store.get_run, run.run_id)
                return durable
            await self._preserve_ambiguous_launch(context, run.run_id)
            raise _AmbiguousLaunchError(exc) from exc

    async def _preserve_ambiguous_launch(self, context: ToolRuntimeContext, run_id: str) -> None:
        try:
            await self.cancel(
                context,
                run_id=run_id,
                force=True,
                reason="Background script launch outcome is indeterminate.",
            )
        except BaseException:
            logger.warning("script_ambiguous_launch_cancel_pending", run_id=run_id, exc_info=True)

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
        ready = await asyncio.to_thread(self.store.get_run, run.run_id)
        if ready.cancel_requested_at is not None:
            return await self._complete_cancel_before_spawn(context, ready)
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
        supervisor_handle = _require_supervisor_handle(run.supervisor_handle)
        try:
            message = await run_command_via_supervisor(
                socket_path,
                namespace=_local_namespace(run.run_id),
                argv=[sys.executable, "-m", "mindroom.script_runs.shim", str(source_path), str(token_path)],
                env=environment,
                cwd=str(workspace),
                tail=200,
                timeout=0,
                handle=supervisor_handle,
            )
            _validate_local_launch_message(message, expected_handle=supervisor_handle)
            launched = await asyncio.to_thread(self.store.get_run, run.run_id)
            if launched.cancel_requested_at is not None:
                await self._preserve_ambiguous_launch(context, run.run_id)
                return await asyncio.to_thread(self.store.get_run, run.run_id)
            return await asyncio.to_thread(
                self.store.transition_run,
                run.run_id,
                state=ScriptRunState.RUNNING,
                supervisor_handle=supervisor_handle,
            )
        except BaseException as exc:
            durable: ScriptRunRecord | None = None
            with suppress(Exception):
                durable = await asyncio.to_thread(self.store.get_run, run.run_id)
            if durable is not None and durable.cancel_requested_at is not None:
                if durable.state not in _TERMINAL_STATES:
                    await self._preserve_ambiguous_launch(context, run.run_id)
                    durable = await asyncio.to_thread(self.store.get_run, run.run_id)
                return durable
            await self._preserve_ambiguous_launch(context, run.run_id)
            raise _AmbiguousLaunchError(exc) from exc

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
        worker = await self._worker_handle(run)
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
            await self._cleanup_token(context, terminal)
        return terminal

    async def _terminate_and_confirm(
        self,
        run: ScriptRunRecord,
        *,
        force: bool,
    ) -> WorkerScriptStatus | None:
        status, signal_error = await self._signal_and_wait(run, force=force)
        if status.state == "exited":
            return status
        if force or status.state != "running":
            if signal_error is not None:
                raise signal_error
            return status
        forced_status, force_error = await self._signal_and_wait(run, force=True)
        if forced_status.state == "exited":
            return forced_status
        if signal_error is not None:
            raise signal_error
        if force_error is not None:
            raise force_error
        return forced_status

    async def _signal_and_wait(
        self,
        run: ScriptRunRecord,
        *,
        force: bool,
    ) -> tuple[WorkerScriptStatus, BaseException | None]:
        signal_error: BaseException | None = None
        try:
            receipt = await self._signal_process(run, force=force)
            _validate_cancel_receipt(receipt)
        except BaseException as exc:
            signal_error = exc
        try:
            status = await self._wait_for_process_exit(run)
        except BaseException as status_error:
            if signal_error is not None:
                raise signal_error from status_error
            raise
        return status, signal_error

    async def _wait_for_process_exit(self, run: ScriptRunRecord) -> WorkerScriptStatus:
        deadline = asyncio.get_running_loop().time() + self.cancellation_grace_seconds
        while True:
            status = await self._process_status(run)
            if status.state != "running":
                return status
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return status
            await asyncio.sleep(min(self.cancellation_poll_interval_seconds, remaining))

    async def _signal_process(self, run: ScriptRunRecord, *, force: bool) -> WorkerScriptCancel:
        if run.supervisor_handle is None:
            return WorkerScriptCancel(cancel_requested=False, already_finished=False, unknown_handle=True)
        if run.local_unsafe:
            message = await asyncio.to_thread(
                kill_command_via_supervisor,
                ensure_shell_supervisor(),
                namespace=_local_namespace(run.run_id),
                handle=run.supervisor_handle,
                force=force,
            )
            return _parse_local_cancel(message)
        worker = await self._worker_handle(run)
        if worker is None:
            return WorkerScriptCancel(cancel_requested=False, already_finished=False, unknown_handle=True)
        return await self.worker_client.cancel(
            worker,
            run_id=run.run_id,
            supervisor_handle=run.supervisor_handle,
            force=force,
        )

    async def _worker_handle(self, run: ScriptRunRecord) -> WorkerHandle | None:
        if self.worker_backend is None or run.worker_id is None or run.worker_key is None:
            return None
        workers = await asyncio.to_thread(self.worker_backend.list_workers, include_idle=True)
        return next(
            (worker for worker in workers if worker.worker_id == run.worker_id and worker.worker_key == run.worker_key),
            None,
        )

    async def _cleanup_token(self, context: ToolRuntimeContext, run: ScriptRunRecord) -> None:
        try:
            workspace: Path | None
            if run.local_unsafe:
                workspace = _agent_workspace(context)
            else:
                worker = await self._worker_handle(run)
                workspace = (
                    _worker_workspace(context, worker)
                    if worker is not None
                    else _worker_workspace_from_run(context, run)
                )
            if workspace is None:
                return
            await asyncio.to_thread(_remove_snapshot_token, workspace, run.run_id)
        except Exception:
            logger.warning("script_capability_cleanup_failed", run_id=run.run_id, exc_info=True)


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
    return workspace if workspace.exists() else None


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
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current_descriptor = os.open(workspace, directory_flags)
        descriptors.append(current_descriptor)
        for part in _snapshot_relative_dir(run_id).parts:
            current_descriptor = os.open(part, directory_flags, dir_fd=current_descriptor)
            descriptors.append(current_descriptor)
        try:
            metadata = os.stat("capability", dir_fd=current_descriptor, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            return
        os.unlink("capability", dir_fd=current_descriptor)
    except OSError:
        return
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _parse_local_status(message: str) -> WorkerScriptStatus:
    if message.startswith("Status: RUNNING"):
        return WorkerScriptStatus(state="running", output=message)
    finished = _FINISHED_RE.match(message)
    if finished is not None:
        return WorkerScriptStatus(state="exited", output=message, exit_code=int(finished.group(1)))
    return WorkerScriptStatus.unknown_handle()


def _validate_local_launch_message(message: str, *, expected_handle: str) -> None:
    match = _HANDLE_RE.search(message)
    if match is None or match.group(0) != expected_handle:
        raise ScriptRunManagerError(message)


def _parse_local_cancel(message: str) -> WorkerScriptCancel:
    if message.startswith(("Terminated process", "Force-killed process")):
        return WorkerScriptCancel(cancel_requested=True, already_finished=False, unknown_handle=False)
    if message.startswith(("Process already finished", "Process ")):
        return WorkerScriptCancel(cancel_requested=False, already_finished=True, unknown_handle=False)
    if message.startswith("Error: Unknown handle"):
        return WorkerScriptCancel(cancel_requested=False, already_finished=False, unknown_handle=True)
    raise ScriptRunManagerError(message)


def _validate_cancel_receipt(receipt: WorkerScriptCancel) -> None:
    if receipt.cancel_requested or receipt.already_finished or receipt.unknown_handle:
        return
    msg = "Worker returned an empty script cancellation receipt."
    raise ScriptRunManagerError(msg)


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


def _require_worker_spec(worker_spec: WorkerSpec | None) -> WorkerSpec:
    if worker_spec is None:
        msg = "Background script worker specification is unavailable."
        raise ScriptRunManagerError(msg)
    return worker_spec


def _require_supervisor_handle(supervisor_handle: str | None) -> str:
    if supervisor_handle is None:
        msg = "Background script supervisor handle is unavailable."
        raise ScriptRunManagerError(msg)
    return supervisor_handle
