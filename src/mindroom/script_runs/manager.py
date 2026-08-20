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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from weakref import WeakValueDictionary

from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
from mindroom.constants import CONTROL_STATE_PATH_ENV
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.script_runs.models import (
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
    script_worker_key_belongs_to_run,
    script_worker_key_for_run,
    supervisor_handle_for_run,
)
from mindroom.script_runs.policy import resolve_script_launch_grants
from mindroom.script_runs.reasons import (
    AMBIGUOUS_LAUNCH,
    INTERRUPTION_REASONS,
    MAX_RUNTIME_EXCEEDED,
    PROCESS_EXIT_OBSERVED,
    SUPERVISOR_UNAVAILABLE,
)
from mindroom.script_runs.store import ScriptRunNotFoundError, ScriptRunStore, mint_script_capability
from mindroom.script_runs.worker_client import ScriptWorkerClient, WorkerScriptCancel, WorkerScriptStatus
from mindroom.shell_supervisor import (
    background_script_supervision_supported,
    check_command_via_supervisor,
    ensure_shell_supervisor,
    kill_command_via_supervisor,
    parse_shell_supervisor_status,
    run_command_via_supervisor,
)
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config
from mindroom.tool_system.worker_routing import (
    agent_workspace_root_path,
    build_agent_toolkit_worker_target,
    serialize_tool_execution_identity,
)
from mindroom.workers.backends.static_runner import StaticSandboxRunnerBackend
from mindroom.workers.models import WorkerHandle, WorkerSpec
from mindroom.workers.worker_retirement import remove_directory_tree_at
from mindroom.workspaces import resolve_workspace_relative_path

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workers.backend import WorkerBackend

__all__ = [
    "ScriptRunLimits",
    "ScriptRunManager",
    "ScriptRunManagerError",
    "ScriptRunStatus",
    "script_execution_uses_worker",
]

logger = get_logger(__name__)

_MAX_SOURCE_BYTES = 128 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024
_LOCAL_EXECUTION_MODES = frozenset({"off", "local", "disabled"})
_WORKER_EXECUTION_MODES = frozenset({"all", "sandbox_all", "selective", "sandbox_selective"})
_HANDLE_RE = re.compile(r"shell:[0-9a-f]{32}")
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


@runtime_checkable
class _RetiringWorkerBackend(Protocol):
    backend_name: str
    cleanup_locator: str | None

    def retire_worker(self, worker_key: str) -> None:
        """Destructively retire one exact script-owned worker."""


def script_execution_uses_worker(
    runtime_paths: RuntimePaths,
    *,
    worker_backend_configured: bool = False,
) -> bool:
    """Return whether configured script execution leaves the primary process."""
    proxy_config = sandbox_proxy_config(runtime_paths)
    if proxy_config.execution_mode in _LOCAL_EXECUTION_MODES:
        return False
    if proxy_config.execution_mode in _WORKER_EXECUTION_MODES:
        return True
    return proxy_config.execution_mode is None and (worker_backend_configured or proxy_config.proxy_url is not None)


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
class ScriptRunStatus:
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
    _run_locks: WeakValueDictionary[str, asyncio.Lock] = field(
        default_factory=WeakValueDictionary,
        init=False,
        repr=False,
    )
    _launch_admission_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _launches_drained: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _launches_in_progress: int = field(default=0, init=False)
    _launch_admission_closed: bool = field(default=False, init=False)
    _startup_reconciliation_owners: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        """Mark the empty admission set as drained before any launch begins."""
        self._launches_drained.set()

    async def begin_shutdown(self) -> None:
        """Permanently reject new launches and drain every already-admitted launch."""
        async with self._launch_admission_lock:
            self._launch_admission_closed = True
            if self._launches_in_progress == 0:
                self._launches_drained.set()
        await self._launches_drained.wait()

    async def begin_startup_reconciliation(self) -> None:
        """Fence all launches until inherited durable ownership is revoked and retired."""
        async with self._launch_admission_lock:
            self._startup_reconciliation_owners += 1
            if self._launches_in_progress == 0:
                self._launches_drained.set()
        await self._launches_drained.wait()

    async def end_startup_reconciliation(self) -> None:
        """Reopen launch admission only after startup cleanup is durably complete."""
        async with self._launch_admission_lock:
            if self._startup_reconciliation_owners <= 0:
                msg = "Background script runtime reconciliation is not active."
                raise ScriptRunManagerError(msg)
            self._startup_reconciliation_owners -= 1

    async def _admit_launch(self) -> None:
        async with self._launch_admission_lock:
            if self._launch_admission_closed:
                msg = "Background script runtime is shutting down."
                raise ScriptRunManagerError(msg)
            if self._startup_reconciliation_owners:
                msg = "Background script runtime reconciliation is in progress."
                raise ScriptRunManagerError(msg)
            self._launches_in_progress += 1
            self._launches_drained.clear()

    async def _release_launch_admission(self) -> None:
        async with self._launch_admission_lock:
            self._launches_in_progress -= 1
            if self._launches_in_progress == 0:
                self._launches_drained.set()

    async def run(  # noqa: PLR0915
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
        source_bytes = await asyncio.to_thread(self._resolve_source, context, source=source, path=path)
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
        worker_backend = self._worker_backend_for(None)
        if script_execution_uses_worker(
            context.runtime_paths,
            worker_backend_configured=worker_backend is not None,
        ):
            if not self.gateway_url:
                msg = "Background-script workers require MINDROOM_SCRIPT_GATEWAY_URL or MINDROOM_PUBLIC_URL."
                raise ScriptRunManagerError(msg)
            worker_backend = _require_script_worker_backend(worker_backend)
            if worker_target.worker_key is None:
                msg = "Background script worker scope could not be resolved for this requester."
                raise ScriptRunManagerError(msg)
            if worker_target.private_agent_names:
                msg = "Background scripts in dedicated workers are not supported for private agents."
                raise ScriptRunManagerError(msg)
            run_id = f"script-{uuid.uuid4().hex}"
            worker_key = script_worker_key_for_run(worker_target.worker_key, run_id)
            local_unsafe = False
        elif execution_mode in _LOCAL_EXECUTION_MODES:
            if not background_script_supervision_supported():
                msg = "Background scripts require Linux process-group containment."
                raise ScriptRunManagerError(msg)
            run_id = f"script-{uuid.uuid4().hex}"
            worker_key = None
            local_unsafe = True
        else:
            msg = "Background scripts require a worker or an explicitly disabled sandbox."
            raise ScriptRunManagerError(msg)

        launch_grants = await asyncio.to_thread(self.grant_resolver, context)
        if effective_limits.allowed_tools is not None:
            allowed_tools = frozenset(effective_limits.allowed_tools)
            unknown_toolkits = allowed_tools - {grant.toolkit_name for grant in launch_grants}
            if unknown_toolkits:
                names = ", ".join(sorted(unknown_toolkits))
                msg = (
                    f"Background script allowed_tools contains unknown toolkit names or unavailable toolkits: {names}."
                )
                raise ScriptRunManagerError(msg)
            launch_grants = tuple(grant for grant in launch_grants if grant.toolkit_name in allowed_tools)
        token, token_hash = mint_script_capability()
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
            preapprove_launch_grants=effective_limits.allowed_tools is not None,
            worker_key=worker_key,
            worker_backend_locator=None,
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
                mirrored_credential_services=frozenset(),
            )
        )
        await self._admit_launch()
        try:
            if local_unsafe:
                return await self._create_and_launch(
                    context,
                    run=run,
                    source=source_bytes,
                    token=token,
                    max_concurrent_runs=effective_limits.max_concurrent_runs,
                    worker_spec=worker_spec,
                )
            admitted_backend = _require_script_worker_backend(self._worker_backend_for(None))
            run = replace(run, worker_backend_locator=admitted_backend.cleanup_locator)
            return await self._create_and_launch(
                context,
                run=run,
                source=source_bytes,
                token=token,
                max_concurrent_runs=effective_limits.max_concurrent_runs,
                worker_spec=worker_spec,
            )
        finally:
            await self._release_launch_admission()

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
        run_lock = self._run_lock(run.run_id)
        async with run_lock:
            try:
                async with self._launch_lock:
                    active = await asyncio.to_thread(
                        self.store.list_runs,
                        agent_name=context.agent_name,
                        owner_user_id=context.requester_id,
                        include_finished=False,
                    )
                    if len(active) >= max_concurrent_runs:
                        _raise_concurrent_run_limit()
                    await run_blocking_until_complete(self.store.create_run, run)
                created = await asyncio.to_thread(self.store.get_run, run.run_id)
                if created.cancel_requested_at is not None:
                    return await self._complete_cancel_before_spawn(created)
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
                raise exc.cause from None
            except BaseException as exc:
                await run_coroutine_until_complete(self._finalize_failed_launch(run, exc))
                raise

    async def _finalize_failed_launch(self, run: ScriptRunRecord, failure: BaseException) -> None:
        try:
            durable = await asyncio.to_thread(self.store.get_run, run.run_id)
        except ScriptRunNotFoundError:
            return
        if durable.state in _TERMINAL_STATES:
            return
        if durable.cancel_requested_at is not None:
            failure_state = _terminal_state_for(durable)
        else:
            failure_state = (
                ScriptRunState.INTERRUPTED if isinstance(failure, asyncio.CancelledError) else ScriptRunState.FAILED
            )
            durable = await asyncio.to_thread(
                self.store.request_cancel,
                run.run_id,
                reason=_bounded_error(failure),
            )
        await self.broker.cancel_run(run.run_id)
        await self._cleanup_owned_resources(durable)
        await asyncio.to_thread(
            self.store.finalize_cleaned_run,
            run.run_id,
            state=failure_state,
            error=_bounded_error(failure),
        )

    async def _complete_cancel_before_spawn(
        self,
        run: ScriptRunRecord,
    ) -> ScriptRunRecord:
        if run.state in _TERMINAL_STATES:
            return run
        await self.broker.cancel_run(run.run_id)
        await self._cleanup_owned_resources(run)
        return await asyncio.to_thread(
            self.store.finalize_cleaned_run,
            run.run_id,
            state=_terminal_state_for(run),
        )

    async def status(
        self,
        context: ToolRuntimeContext,
        *,
        run_id: str,
    ) -> ScriptRunStatus:
        """Return one owned run after reconciling its supervisor state."""
        async with self._run_lock(run_id):
            run = await self._owned_run(context, run_id)
            return await self._status_locked(context, run)

    async def _status_locked(
        self,
        context: ToolRuntimeContext,
        run: ScriptRunRecord,
    ) -> ScriptRunStatus:
        """Return status while launch allocation and cleanup are excluded."""
        if run.state in _TERMINAL_STATES:
            return ScriptRunStatus(run=run, output=run.output)
        if run.cancel_requested_at is not None:
            try:
                reconciled = await self._terminate_durable_run_locked(
                    run,
                    reason=run.cancellation_reason or "Cancellation requested by the owning agent.",
                )
            except ScriptRunManagerError:
                pending = await self._owned_run(context, run.run_id)
                if pending.finished_at is not None:
                    return ScriptRunStatus(run=pending, output=pending.output)
                status = await self._process_status(pending)
                return ScriptRunStatus(run=pending, output=status.output)
            return ScriptRunStatus(run=reconciled, output=reconciled.output)
        if _runtime_expired(run):
            reconciled = await self._terminate_durable_run_locked(
                run,
                reason=MAX_RUNTIME_EXCEEDED,
            )
            return ScriptRunStatus(run=reconciled, output=reconciled.output)
        status = await self._process_status(run)
        reconciled = await self._apply_process_status(run, status)
        return ScriptRunStatus(
            run=reconciled,
            output=reconciled.output if reconciled.state in _TERMINAL_STATES else status.output,
        )

    async def cancel(
        self,
        context: ToolRuntimeContext,
        *,
        run_id: str,
        force: bool = False,
        reason: str = "Cancellation requested by the owning agent.",
    ) -> ScriptRunRecord:
        """Revoke one run durably before signalling its existing supervisor."""
        return await self._terminate_run(
            context,
            run_id=run_id,
            force=force,
            reason=reason,
        )

    async def revoke(self, run_id: str, *, reason: str) -> ScriptRunRecord:
        """Persist lifecycle revocation and cancel broker ownership without a live bot."""
        run = await asyncio.to_thread(self.store.get_run, run_id)
        if run.state in _TERMINAL_STATES:
            await self.broker.cancel_run(run_id)
            return run
        revoked = await asyncio.to_thread(self.request_revocation, run.run_id, reason=reason)
        await self.broker.cancel_run(run_id)
        return revoked

    def request_revocation(self, run_id: str, *, reason: str) -> ScriptRunRecord:
        """Persist lifecycle desired state before any broker or supervisor work."""
        return self.store.request_cancel(run_id, reason=reason)

    async def _terminate_run(
        self,
        context: ToolRuntimeContext,
        *,
        run_id: str,
        force: bool,
        reason: str,
    ) -> ScriptRunRecord:
        """Revoke, signal, and publish one confirmed terminal process outcome."""
        run = await self._owned_run(context, run_id)
        if run.state not in _TERMINAL_STATES:
            run = await asyncio.to_thread(self.store.request_cancel, run_id, reason=reason)
        return await self._terminate_durable_run(
            run,
            force=force,
            reason=reason,
        )

    async def _terminate_durable_run(
        self,
        run: ScriptRunRecord,
        *,
        force: bool,
        reason: str,
        broker_revoked: bool = False,
    ) -> ScriptRunRecord:
        async with self._run_lock(run.run_id):
            run = await asyncio.to_thread(self.store.get_run, run.run_id)
            return await self._terminate_durable_run_locked(
                run,
                force=force,
                reason=reason,
                broker_revoked=broker_revoked,
            )

    async def _terminate_durable_run_locked(
        self,
        run: ScriptRunRecord,
        *,
        force: bool = False,
        reason: str,
        broker_revoked: bool = False,
    ) -> ScriptRunRecord:
        run = await asyncio.to_thread(self.store.get_run, run.run_id)
        if run.state in _TERMINAL_STATES:
            return run
        if run.finished_at is not None:
            return await self._finalize_observed_exit(run, broker_revoked=broker_revoked)
        revoked = await asyncio.to_thread(self.store.request_cancel, run.run_id, reason=reason)
        broker_error: BaseException | None = None
        process_error: BaseException | None = None
        if not broker_revoked:
            try:
                await self.broker.cancel_run(run.run_id)
            except BaseException as exc:
                broker_error = exc
        try:
            revoked = await self._reconcile_revoked_process_run(revoked, force=force)
        except BaseException as exc:
            process_error = exc
        if process_error is not None:
            raise process_error
        if broker_error is not None:
            raise broker_error
        return await self._finalize_observed_exit(revoked, broker_revoked=True)

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

    async def reconcile_durable(
        self,
        *,
        run_id: str,
        broker_revoked: bool = False,
    ) -> ScriptRunRecord:
        """Reconcile process truth for one trusted durable lifecycle record."""
        async with self._run_lock(run_id):
            run = await asyncio.to_thread(self.store.get_run, run_id)
            return await self._reconcile_durable_run_locked(run, broker_revoked=broker_revoked)

    async def reconcile_revoked_process(self, *, run_id: str) -> ScriptRunRecord:
        """Record process truth for one already-revoked run without broker or resource cleanup."""
        async with self._run_lock(run_id):
            run = await asyncio.to_thread(self.store.get_run, run_id)
            if run.state in _TERMINAL_STATES or run.finished_at is not None:
                return run
            if run.cancel_requested_at is None:
                msg = "Background script process-only reconciliation requires durable revocation."
                raise ScriptRunManagerError(msg)
            return await self._reconcile_revoked_process_run(run, force=False)

    async def _reconcile_durable_run_locked(
        self,
        run: ScriptRunRecord,
        *,
        broker_revoked: bool = False,
    ) -> ScriptRunRecord:
        if run.state in _TERMINAL_STATES:
            return run
        if run.finished_at is not None:
            return await self._finalize_observed_exit(run, broker_revoked=broker_revoked)
        if run.cancel_requested_at is not None:
            return await self._terminate_durable_run_locked(
                run,
                force=False,
                reason=run.cancellation_reason or "Cancellation requested by the owning agent.",
                broker_revoked=broker_revoked,
            )
        if _runtime_expired(run):
            return await self._terminate_durable_run_locked(
                run,
                force=False,
                reason=MAX_RUNTIME_EXCEEDED,
            )
        status = await self._process_status(run)
        return await self._apply_process_status(run, status)

    async def _launch_worker(
        self,
        context: ToolRuntimeContext,
        *,
        run: ScriptRunRecord,
        source: bytes,
        token: str,
        worker_spec: WorkerSpec,
    ) -> ScriptRunRecord:
        backend = self.worker_backend
        if backend is None:
            msg = "Background script worker backend is unavailable."
            raise ScriptRunManagerError(msg)
        worker = await asyncio.to_thread(backend.ensure_worker, worker_spec)
        await asyncio.to_thread(
            self.store.transition_run,
            run.run_id,
            state=ScriptRunState.STARTING,
            worker_id=worker.worker_id,
        )
        assigned = await asyncio.to_thread(self.store.get_run, run.run_id)
        if assigned.cancel_requested_at is not None:
            return await self._complete_cancel_before_spawn(assigned)
        workspace = _worker_workspace(context, worker)
        await self._record_snapshot_locator(run, workspace)
        await asyncio.to_thread(_write_snapshot, workspace, run.run_id, source=source, token=token)
        ready = await asyncio.to_thread(self.store.get_run, run.run_id)
        if ready.cancel_requested_at is not None:
            return await self._complete_cancel_before_spawn(ready)
        try:
            await self.worker_client.launch(
                worker,
                run_id=run.run_id,
                source_digest=run.source_digest,
                gateway_url=self.gateway_url,
                private_agent_names=(
                    tuple(sorted(worker_spec.private_agent_names))
                    if worker_spec.private_agent_names is not None
                    else None
                ),
            )
        except BaseException as exc:
            await self._preserve_ambiguous_launch(context, run.run_id)
            raise _AmbiguousLaunchError(exc) from exc
        return await self._settle_spawned_run(
            context,
            run,
            worker_id=worker.worker_id,
        )

    async def _settle_spawned_run(
        self,
        context: ToolRuntimeContext,
        run: ScriptRunRecord,
        *,
        worker_id: str | None = None,
    ) -> ScriptRunRecord:
        """Publish one spawned process or preserve a concurrent cancellation."""
        try:
            launched = await asyncio.to_thread(self.store.get_run, run.run_id)
            if launched.cancel_requested_at is not None:
                await self._preserve_ambiguous_launch(context, run.run_id)
                return await asyncio.to_thread(self.store.get_run, run.run_id)
            return await asyncio.to_thread(
                self.store.transition_run,
                run.run_id,
                state=ScriptRunState.RUNNING,
                worker_id=worker_id,
            )
        except asyncio.CancelledError as exc:
            with suppress(_AmbiguousLaunchError):
                await run_coroutine_until_complete(
                    self._resolve_ambiguous_launch_failure(context, run.run_id, exc),
                )
            raise
        except BaseException as exc:
            return await self._resolve_ambiguous_launch_failure(context, run.run_id, exc)

    async def _resolve_ambiguous_launch_failure(
        self,
        context: ToolRuntimeContext,
        run_id: str,
        failure: BaseException,
    ) -> ScriptRunRecord:
        durable: ScriptRunRecord | None = None
        with suppress(Exception):
            durable = await asyncio.to_thread(self.store.get_run, run_id)
        if durable is not None and durable.cancel_requested_at is not None:
            if durable.state not in _TERMINAL_STATES:
                await self._preserve_ambiguous_launch(context, run_id)
                durable = await asyncio.to_thread(self.store.get_run, run_id)
            return durable
        await self._preserve_ambiguous_launch(context, run_id)
        raise _AmbiguousLaunchError(failure) from failure

    async def _preserve_ambiguous_launch(self, context: ToolRuntimeContext, run_id: str) -> None:
        try:
            run = await self._owned_run(context, run_id)
            await self._terminate_durable_run_locked(
                run,
                force=True,
                reason=AMBIGUOUS_LAUNCH,
            )
        except asyncio.CancelledError:
            raise
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
        await self._record_snapshot_locator(run, workspace)
        source_path, token_path = await asyncio.to_thread(
            _write_snapshot,
            workspace,
            run.run_id,
            source=source,
            token=token,
        )
        ready = await asyncio.to_thread(self.store.get_run, run.run_id)
        if ready.cancel_requested_at is not None:
            return await self._complete_cancel_before_spawn(ready)
        socket_path = await asyncio.to_thread(ensure_shell_supervisor)
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
        supervisor_handle = supervisor_handle_for_run(run.run_id)
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
        except BaseException as exc:
            return await self._resolve_ambiguous_launch_failure(context, run.run_id, exc)
        return await self._settle_spawned_run(context, run)

    async def _owned_run(self, context: ToolRuntimeContext, run_id: str) -> ScriptRunRecord:
        not_found = f"Background script '{run_id}' was not found."
        try:
            run = await asyncio.to_thread(self.store.get_run, run_id)
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
                source_bytes = _read_workspace_source(workspace, path or "")
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
        supervisor_handle = supervisor_handle_for_run(run.run_id)
        if run.local_unsafe:
            socket_path = await asyncio.to_thread(ensure_shell_supervisor)
            message = await asyncio.to_thread(
                check_command_via_supervisor,
                socket_path,
                namespace=_local_namespace(run.run_id),
                handle=supervisor_handle,
            )
            return _parse_local_status(message)
        worker = await self._worker_handle(run)
        if worker is None:
            return WorkerScriptStatus.unknown_handle()
        return await self.worker_client.status(
            worker,
            run_id=run.run_id,
        )

    async def _apply_process_status(
        self,
        run: ScriptRunRecord,
        status: WorkerScriptStatus,
    ) -> ScriptRunRecord:
        if status.state == "running":
            backend = self._worker_backend_for(run)
            if run.worker_key is not None and backend is not None:
                await asyncio.to_thread(backend.touch_worker, run.worker_key)
            return run
        bounded_output = _bounded_output(status.output)
        if status.state == "unknown":
            reason = SUPERVISOR_UNAVAILABLE
            error = reason
        else:
            reason = PROCESS_EXIT_OBSERVED
            error = (
                None
                if status.exit_code == 0
                else bounded_output or f"Background script exited with code {status.exit_code}."
            )
        observed = await asyncio.to_thread(
            self.store.record_process_exit,
            run.run_id,
            exit_code=status.exit_code,
            error=error,
            output=bounded_output,
            cancellation_reason=reason,
        )
        return await self._finalize_observed_exit(observed)

    async def _reconcile_revoked_process_run(
        self,
        run: ScriptRunRecord,
        *,
        force: bool,
    ) -> ScriptRunRecord:
        """Confirm and record process exit without satisfying broker or cleanup obligations."""
        if run.finished_at is not None:
            return run
        try:
            process_status = await self._terminate_and_confirm(run, force=force)
        except asyncio.CancelledError:
            raise
        except BaseException as process_error:
            try:
                return await self._retire_worker_to_confirm_exit(run)
            except BaseException as retirement_error:
                raise process_error from retirement_error
        if process_status is None or process_status.state == "running":
            msg = "Background script termination is not yet confirmed; retry cancellation."
            raise ScriptRunManagerError(msg)
        return await run_coroutine_until_complete(
            asyncio.to_thread(
                self.store.record_process_exit,
                run.run_id,
                exit_code=process_status.exit_code,
                error=(SUPERVISOR_UNAVAILABLE if process_status.state == "unknown" else None),
                output=_bounded_output(process_status.output),
                cancellation_reason=run.cancellation_reason or "Cancellation requested by the owning agent.",
            ),
        )

    async def _retire_worker_to_confirm_exit(self, run: ScriptRunRecord) -> ScriptRunRecord:
        """Use exact dedicated-worker deletion as process-death proof when its HTTP runner is unreachable."""
        if run.local_unsafe:
            msg = "Unsafe-local script process exit cannot be confirmed through worker retirement."
            raise ScriptRunManagerError(msg)
        worker_key = run.worker_key
        if worker_key is None or not script_worker_key_belongs_to_run(worker_key, run.run_id):
            msg = "Background script dedicated worker ownership is invalid."
            raise ScriptRunManagerError(msg)
        backend = self._worker_backend_for(run)
        if backend is None:
            msg = "Background script worker backend is unavailable; retry process reconciliation."
            raise ScriptRunManagerError(msg)
        await asyncio.to_thread(_require_script_worker_backend(backend).retire_worker, worker_key)
        return await run_coroutine_until_complete(
            asyncio.to_thread(
                self.store.record_process_exit,
                run.run_id,
                exit_code=None,
                error="Background script worker was retired after its runner became unavailable.",
                output="",
                cancellation_reason=run.cancellation_reason or "Cancellation requested by the owning agent.",
            ),
        )

    async def _finalize_observed_exit(
        self,
        run: ScriptRunRecord,
        *,
        broker_revoked: bool = False,
    ) -> ScriptRunRecord:
        """Clean exact durable ownership before publishing an observed terminal outcome."""
        if run.finished_at is None:
            msg = "Background script process exit has not been observed durably."
            raise ScriptRunManagerError(msg)
        if not broker_revoked:
            await self.broker.cancel_run(run.run_id)
        await self._cleanup_owned_resources(run)
        return await asyncio.to_thread(
            self.store.finalize_cleaned_run,
            run.run_id,
            state=_terminal_state_for(run),
        )

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
        except asyncio.CancelledError:
            raise
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
        supervisor_handle = supervisor_handle_for_run(run.run_id)
        if run.local_unsafe:
            socket_path = await asyncio.to_thread(ensure_shell_supervisor)
            message = await asyncio.to_thread(
                kill_command_via_supervisor,
                socket_path,
                namespace=_local_namespace(run.run_id),
                handle=supervisor_handle,
                force=force,
            )
            return _parse_local_cancel(message)
        worker = await self._worker_handle(run)
        if worker is None:
            return WorkerScriptCancel(cancel_requested=False, already_finished=False, unknown_handle=True)
        return await self.worker_client.cancel(
            worker,
            run_id=run.run_id,
            force=force,
        )

    async def _worker_handle(self, run: ScriptRunRecord) -> WorkerHandle | None:
        backend = self._worker_backend_for(run)
        if backend is None and not run.local_unsafe and run.worker_id is not None:
            msg = "Background script worker backend is unavailable; retry reconciliation."
            raise ScriptRunManagerError(msg)
        if backend is None or run.worker_id is None or run.worker_key is None:
            return None
        workers = await asyncio.to_thread(backend.list_workers, include_idle=True)
        return next(
            (worker for worker in workers if worker.worker_id == run.worker_id and worker.worker_key == run.worker_key),
            None,
        )

    def _worker_backend_for(self, run: ScriptRunRecord | None) -> WorkerBackend | None:
        backend = self.worker_backend
        if backend is None or run is None:
            return backend
        if run.worker_backend_locator is None or backend.cleanup_locator != run.worker_backend_locator:
            return None
        return backend

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        lock = self._run_locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._run_locks[run_id] = lock
        return lock

    async def _record_snapshot_locator(self, run: ScriptRunRecord, workspace: Path) -> ScriptRunRecord:
        locator = _snapshot_locator(self.store.storage_root, workspace, run.run_id)
        return await asyncio.to_thread(self.store.record_snapshot_locator, run.run_id, locator)

    async def _cleanup_owned_resources(self, run: ScriptRunRecord) -> None:
        if run.local_unsafe:
            if run.worker_key is not None or run.worker_id is not None:
                msg = "Unsafe-local script run cannot own a dedicated worker."
                raise ScriptRunManagerError(msg)
            if run.snapshot_locator is not None:
                cleaned = await asyncio.to_thread(_remove_snapshot, self.store.storage_root, run.snapshot_locator)
                if not cleaned:
                    msg = "Background script snapshot cleanup is pending."
                    raise ScriptRunManagerError(msg)
            return
        worker_key = run.worker_key
        if worker_key is None or not script_worker_key_belongs_to_run(worker_key, run.run_id):
            msg = "Background script dedicated worker ownership is invalid."
            raise ScriptRunManagerError(msg)
        backend = self._worker_backend_for(run)
        if backend is None:
            msg = "Background script worker backend is unavailable; retry cleanup."
            raise ScriptRunManagerError(msg)
        await asyncio.to_thread(_require_script_worker_backend(backend).retire_worker, worker_key)


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
        msg = "Background script worker must expose a primary-visible state root or subpath."
        raise ScriptRunManagerError(msg)
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


def _snapshot_relative_dir(run_id: str) -> Path:
    return Path(".mindroom") / "script-runs" / run_id


def _read_workspace_source(workspace: Path, relative_path: str) -> bytes:
    """Read one bounded regular file through no-follow workspace descriptors."""
    relative = Path(relative_path)
    if relative.is_absolute() or relative == Path() or ".." in relative.parts:
        msg = "Script source path must stay within the workspace root."
        raise ValueError(msg)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current_descriptor = os.open(workspace, directory_flags)
        descriptors.append(current_descriptor)
        for part in relative.parts[:-1]:
            current_descriptor = os.open(part, directory_flags, dir_fd=current_descriptor)
            descriptors.append(current_descriptor)
        source_descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current_descriptor,
        )
        descriptors.append(source_descriptor)
        metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            msg = "Script source path must be a regular file in the agent workspace."
            raise ValueError(msg)
        chunks: list[bytes] = []
        remaining = _MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(source_descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)


def _snapshot_locator(storage_root: Path, workspace: Path, run_id: str) -> str:
    run_dir = (workspace / _snapshot_relative_dir(run_id)).resolve()
    try:
        return run_dir.relative_to(storage_root).as_posix()
    except ValueError as exc:
        msg = "Background script snapshot must stay inside primary storage."
        raise ScriptRunManagerError(msg) from exc


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


def _remove_snapshot(storage_root: Path, locator: str) -> bool:
    """Recursively remove one descriptor-bound run snapshot without following symlinks."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current_descriptor = os.open(storage_root, directory_flags)
        descriptors.append(current_descriptor)
        parts = Path(locator).parts
        for part in parts[:-1]:
            current_descriptor = os.open(part, directory_flags, dir_fd=current_descriptor)
            descriptors.append(current_descriptor)
        remove_directory_tree_at(current_descriptor, parts[-1])
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
    return True


def _parse_local_status(message: str) -> WorkerScriptStatus:
    status = parse_shell_supervisor_status(message)
    if status.state == "error":
        raise ScriptRunManagerError(status.output)
    return WorkerScriptStatus(
        state=status.state,
        output=status.output,
        exit_code=status.exit_code,
    )


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


def _bounded_output(output: str) -> str:
    encoded = output.encode("utf-8")
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return output
    return encoded[-_MAX_OUTPUT_BYTES:].decode("utf-8", errors="ignore")


def _raise_concurrent_run_limit() -> None:
    msg = "Background script concurrent-run limit exceeded."
    raise ScriptRunManagerError(msg)


def _require_script_worker_backend(backend: WorkerBackend | None) -> _RetiringWorkerBackend:
    if backend is None:
        msg = "Background script worker backend is unavailable."
        raise ScriptRunManagerError(msg)
    if isinstance(backend, StaticSandboxRunnerBackend):
        msg = (
            "Background scripts cannot use the shared static sandbox runner; configure explicit "
            "unsafe-local mode or a Docker worker backend."
        )
        raise ScriptRunManagerError(msg)
    if backend.backend_name == "kubernetes":
        msg = (
            "Kubernetes background scripts require a gateway-only listener; "
            "use Docker or explicit unsafe-local mode until that boundary is configured."
        )
        raise ScriptRunManagerError(msg)
    if backend.cleanup_locator is None:
        msg = "Background script worker backend has no durable cleanup locator."
        raise ScriptRunManagerError(msg)
    if not isinstance(backend, _RetiringWorkerBackend):
        msg = "Background script worker backend does not support exact worker retirement."
        raise ScriptRunManagerError(msg)
    return backend


def _terminal_state_for(run: ScriptRunRecord) -> ScriptRunState:
    if run.cancellation_reason in INTERRUPTION_REASONS:
        return ScriptRunState.INTERRUPTED
    if run.finished_at is None or run.cancellation_reason != PROCESS_EXIT_OBSERVED:
        return ScriptRunState.CANCELLED
    return ScriptRunState.EXITED if run.exit_code == 0 else ScriptRunState.FAILED


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
