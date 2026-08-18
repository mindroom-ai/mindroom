"""Focused lifecycle ownership for the primary background-script runtime."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from mindroom import approval_manager
from mindroom.custom_tools.script import bind_script_run_manager
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker, drain_script_tool_cleanup
from mindroom.script_runs.manager import (
    ScriptRunManager,
    ScriptRunManagerError,
    ScriptWorkerBackendBinding,
    script_execution_uses_worker,
)
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunStore, ScriptRunStoreError
from mindroom.script_runs.worker_client import ScriptWorkerClient, ScriptWorkerError
from mindroom.tool_approval import (
    BackgroundScriptToolOrigin,
    ToolApprovalDecision,
    resolve_tool_approval_approver,
)
from mindroom.tool_system.worker_routing import (
    build_agent_toolkit_worker_target,
    parse_tool_execution_identity_payload,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.runtime import primary_worker_backend_is_dedicated

from .runtime import cancel_task, create_logged_task

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.bot import AgentBot
    from mindroom.config.agent import AgentConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import BackgroundApprovalDecision
    from mindroom.orchestration.config_updates import ConfigUpdatePlan
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workers.backend import WorkerBackend

logger = get_logger(__name__)

_REMOVED_AGENT_REASON = "Owning agent was removed by configuration reload."
_ISOLATION_CHANGE_REASON = "Agent isolation changed during configuration reload."
_SCRIPT_RETENTION_SECONDS_ENV = "MINDROOM_SCRIPT_RETENTION_SECONDS"
_DEFAULT_SCRIPT_RETENTION_SECONDS = 30 * 24 * 60 * 60


class _ScriptRuntimeUnavailableError(RuntimeError):
    """The durable script owner has no live runtime generation yet."""


class _BackgroundApprovalManager(Protocol):
    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        room_id: str,
        thread_id: str | None,
        agent_name: str,
        requester_id: str,
        approver_user_id: str,
        tool_name: str,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> BackgroundApprovalDecision: ...

    async def settle_background_approval(
        self,
        origin: BackgroundScriptToolOrigin,
        *,
        reason: str,
    ) -> bool: ...

    async def settle_pending_background_approvals(self, run_id: str, *, reason: str) -> int: ...

    async def prune_background_approvals(self, run_id: str) -> bool: ...


class _WorkerManagerLease(Protocol):
    @property
    def manager(self) -> WorkerBackend: ...

    @property
    def generation_id(self) -> str: ...

    def release(self) -> None: ...


@dataclass(slots=True)
class _WorkerLeaseDelivery:
    """Own a provider lease until the asyncio consumer acknowledges delivery."""

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _lease: _WorkerManagerLease | None = field(default=None, init=False, repr=False)
    _abandoned: bool = field(default=False, init=False, repr=False)

    def acquire(
        self,
        provider: Callable[[], _WorkerManagerLease | None],
    ) -> _WorkerManagerLease | None:
        """Acquire in the executor and release there if delivery was abandoned."""
        lease = provider()
        if lease is None:
            return None
        with self._lock:
            release_lease = self._abandoned
            if not release_lease:
                self._lease = lease
        if release_lease:
            lease.release()
            return None
        return lease

    def acknowledge(self, lease: _WorkerManagerLease) -> bool:
        """Transfer one delivered lease from the handoff to the lifecycle."""
        with self._lock:
            if self._abandoned or self._lease is not lease:
                return False
            self._lease = None
            return True

    def abandon(self) -> _WorkerManagerLease | None:
        """Close delivery and return any lease already waiting for acknowledgement."""
        with self._lock:
            self._abandoned = True
            lease, self._lease = self._lease, None
            return lease

    def settle_task(self, task: asyncio.Task[_WorkerManagerLease | None]) -> None:
        """Settle cancellation or a late provider failure after lifecycle abandonment."""
        if task.cancelled():
            lease = self.abandon()
            if lease is not None:
                _release_worker_lease_later(lease)
            return
        failure = task.exception()
        with self._lock:
            abandoned = self._abandoned
        if failure is not None and abandoned:
            logger.warning(
                "script_worker_backend_pending_acquire_failed",
                exc_info=(type(failure), failure, failure.__traceback__),
            )


@dataclass(slots=True)
class _LiveScriptRuntimeResolver:
    """Rebuild current runtime, worker, and approval authority for a durable run."""

    runtime_paths: RuntimePaths
    bot_provider: Callable[[str], AgentBot | None]
    worker_backend_provider: Callable[[ScriptRunRecord | None], WorkerBackend | None]
    approval_provider: Callable[[], _BackgroundApprovalManager | None] = approval_manager.get_approval_store

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        """Rebuild a context only from the exact durable Matrix execution identity."""
        identity = parse_tool_execution_identity_payload(
            run.execution_identity,
            strict=True,
            error_prefix="Background script execution_identity",
        )
        if (
            identity is None
            or identity.channel != "matrix"
            or identity.agent_name != run.agent_name
            or identity.requester_id != run.owner_user_id
            or identity.room_id != run.room_id
            or identity.resolved_thread_id != run.thread_root_event_id
            or identity.room_id is None
            or identity.session_id is None
        ):
            msg = "Background script execution identity does not match its durable owner."
            raise ValueError(msg)
        bot = self.bot_provider(run.agent_name)
        if bot is None or not bot.running:
            msg = f"Agent runtime '{run.agent_name}' is restarting."
            raise _ScriptRuntimeUnavailableError(msg)
        target = MessageTarget(
            room_id=identity.room_id,
            source_thread_id=identity.thread_id,
            resolved_thread_id=identity.resolved_thread_id,
            reply_to_event_id=None,
            session_id=identity.session_id,
        )
        context = bot._tool_runtime_support.build_context(
            target,
            user_id=identity.requester_id,
            agent_name=identity.agent_name,
            correlation_id=correlation_id,
        )
        if context is None:
            msg = f"Agent runtime '{run.agent_name}' is restarting."
            raise _ScriptRuntimeUnavailableError(msg)
        return context

    def resolve_worker_authority(
        self,
        run: ScriptRunRecord,
        *,
        context: ToolRuntimeContext,
    ) -> ScriptRuntimeWorkerAuthority:
        """Resolve process presence and current configured tool routing independently."""
        worker_id: str | None = None
        if not run.local_unsafe:
            backend = self.worker_backend_provider(run)
            if backend is not None and run.worker_key is not None:
                worker = next(
                    (candidate for candidate in backend.list_workers() if candidate.worker_key == run.worker_key),
                    None,
                )
                worker_id = None if worker is None else worker.worker_id
        config = context.current_config
        agent_config = config.get_agent(context.agent_name)
        worker_target = build_agent_toolkit_worker_target(
            config.resolve_entity(context.agent_name).execution_scope,
            context.agent_name,
            is_private=agent_config.private is not None,
            execution_identity=parse_tool_execution_identity_payload(
                run.execution_identity,
                strict=True,
                error_prefix="Background script execution_identity",
            ),
            runtime_paths=context.runtime_paths,
        )
        return ScriptRuntimeWorkerAuthority(
            worker_id=worker_id,
            local_unsafe=run.local_unsafe,
            worker_target=worker_target,
        )

    async def request_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        context: ToolRuntimeContext,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> ToolApprovalDecision:
        """Await one exact-call decision in the existing Matrix approval domain."""
        approver_user_id = resolve_tool_approval_approver(
            context.current_config,
            context.runtime_paths,
            context.requester_id,
        )
        if approver_user_id is None:
            return ToolApprovalDecision(
                approved=False,
                reason="Background script approval requires a human Matrix requester.",
            )
        approvals = self.approval_provider()
        if approvals is None:
            return ToolApprovalDecision(approved=False, reason="Tool approval runtime is not ready.")
        decision = await approvals.request_background_approval(
            origin=origin,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            agent_name=context.agent_name,
            requester_id=context.requester_id,
            approver_user_id=approver_user_id,
            tool_name=grant.function_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
        )
        return ToolApprovalDecision(
            approved=decision.status == "approved",
            reason=None if decision.status == "approved" else decision.reason,
        )

    async def settle_approval(self, origin: BackgroundScriptToolOrigin, *, reason: str) -> None:
        """Retire an exact card when broker ownership becomes indeterminate."""
        approvals = self.approval_provider()
        if approvals is None:
            msg = "Tool approval runtime is not ready."
            raise _ScriptRuntimeUnavailableError(msg)
        await approvals.settle_background_approval(origin, reason=reason)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        """Retire every pending approval whose broker run ownership ended."""
        approvals = self.approval_provider()
        if approvals is None:
            msg = "Tool approval runtime is not ready."
            raise _ScriptRuntimeUnavailableError(msg)
        await approvals.settle_pending_background_approvals(run_id, reason=reason)

    async def prune_approvals(self, run_id: str) -> bool:
        """Prune settled exact-call targets alongside their retained run."""
        approvals = self.approval_provider()
        if approvals is None:
            return False
        return await approvals.prune_background_approvals(run_id)


@dataclass(slots=True)
class ScriptRuntimeLifecycle:
    """Keep one broker/manager pair stable while runtime generations change."""

    runtime_paths: RuntimePaths
    store: ScriptRunStore
    broker: ScriptToolBroker
    manager: ScriptRunManager
    resolver: _LiveScriptRuntimeResolver
    config_provider: Callable[[], Config | None]
    worker_lease_provider: Callable[[], _WorkerManagerLease | None]
    api_enabled: bool = True
    retention_seconds: float = _DEFAULT_SCRIPT_RETENTION_SECONDS
    pass_timeout_seconds: float = 30.0
    pass_concurrency: int = 4
    reconcile_interval_seconds: float = 30.0
    _api_ready: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _activated_once: bool = field(default=False, init=False, repr=False)
    _start_requested: bool = field(default=False, init=False, repr=False)
    _activation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _worker_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _startup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _maintenance_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _worker_leases: list[_WorkerManagerLease] = field(default_factory=list, init=False, repr=False)
    _current_worker_lease: _WorkerManagerLease | None = field(default=None, init=False, repr=False)
    _worker_config_epoch: int = field(default=0, init=False, repr=False)
    _pending_worker_lease_task: asyncio.Task[_WorkerManagerLease | None] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _pending_worker_lease_delivery: _WorkerLeaseDelivery | None = field(default=None, init=False, repr=False)
    _pending_worker_lease_epoch: int = field(default=-1, init=False, repr=False)

    def bind_api(self, gateway_url: str) -> None:
        """Publish the reachable gateway without replacing the broker that owns calls."""
        self.manager.gateway_url = gateway_url.rstrip("/")
        self._api_ready.set()
        if self.api_enabled and self._start_requested and not self._started and self._startup_task is None:
            self._startup_task = create_logged_task(
                self._activate(),
                name="script_runtime_startup",
                failure_message="Background script runtime startup failed",
            )

    async def start(self) -> None:
        """Bind tools and reconcile after both API and live agent registries exist."""
        self._start_requested = True
        if not self.api_enabled:
            return
        if not self._api_ready.is_set():
            return
        await self._activate()

    async def _activate(self) -> None:
        """Activate exactly once after both composition roots have reported ready."""
        async with self._activation_lock:
            if self._started:
                return
            if not self._start_requested or not self._api_ready.is_set():
                return
            bind_script_run_manager(self.manager)
            self._started = True
            self._activated_once = True
            await self._run_complete_pass(timeout_event="script_startup_pass_timeout")
            self._maintenance_task = create_logged_task(
                self._maintenance_loop(),
                name="script_runtime_maintenance",
                failure_message="Background script runtime maintenance failed",
            )

    async def unbind_api(self) -> None:
        """Withdraw gateway readiness without replacing lifecycle-owned services."""
        self._api_ready.clear()
        self.manager.gateway_url = ""
        startup_task, self._startup_task = self._startup_task, None
        await cancel_task(startup_task)
        maintenance_task, self._maintenance_task = self._maintenance_task, None
        await cancel_task(maintenance_task)
        if self._started:
            bind_script_run_manager(None)
            self._started = False

    async def _maintenance_loop(self) -> None:
        while self._started:
            await asyncio.sleep(self.reconcile_interval_seconds)
            if not self._started:
                return
            try:
                await self._run_complete_pass(timeout_event="script_maintenance_pass_timeout")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("script_maintenance_cycle_failed")

    async def _refresh_worker_backend(self) -> WorkerBackend | None:
        async with self._worker_refresh_lock:
            return await self._refresh_worker_backend_locked()

    async def _refresh_worker_backend_locked(self) -> WorkerBackend | None:
        lease = await self._acquire_current_worker_lease()
        if lease is None:
            self._current_worker_lease = None
            self.manager.worker_backend = None
            self.manager.worker_backend_generation = None
            return None
        existing = next((candidate for candidate in self._worker_leases if candidate.manager is lease.manager), None)
        if existing is not None:
            if lease is not existing:
                await asyncio.to_thread(lease.release)
            self._current_worker_lease = existing
            self.manager.worker_backend = existing.manager
            self.manager.worker_backend_generation = existing.generation_id
            return existing.manager
        self._worker_leases.append(lease)
        self._current_worker_lease = lease
        backend = lease.manager
        self.manager.worker_backend = backend
        self.manager.worker_backend_generation = lease.generation_id
        return backend

    async def _acquire_current_worker_lease(self) -> _WorkerManagerLease | None:
        while True:
            task, delivery, task_epoch = self._get_or_create_worker_lease_acquisition()
            try:
                lease = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._pending_worker_lease_task is task:
                    self._pending_worker_lease_task = None
                    self._pending_worker_lease_delivery = None
                    self._pending_worker_lease_epoch = -1
                raise
            if self._pending_worker_lease_task is task:
                self._pending_worker_lease_task = None
                self._pending_worker_lease_delivery = None
                self._pending_worker_lease_epoch = -1
            if lease is not None and not delivery.acknowledge(lease):
                return None
            if task_epoch == self._worker_config_epoch:
                return lease
            if lease is not None:
                await asyncio.to_thread(lease.release)

    def _get_or_create_worker_lease_acquisition(
        self,
    ) -> tuple[asyncio.Task[_WorkerManagerLease | None], _WorkerLeaseDelivery, int]:
        task = self._pending_worker_lease_task
        if task is not None:
            delivery = self._pending_worker_lease_delivery
            if delivery is None:
                msg = "Pending worker lease acquisition has no delivery owner."
                raise RuntimeError(msg)
            return task, delivery, self._pending_worker_lease_epoch

        delivery = _WorkerLeaseDelivery()
        task = asyncio.create_task(
            asyncio.to_thread(delivery.acquire, self.worker_lease_provider),
            name="script_worker_backend_acquire",
        )
        task.add_done_callback(delivery.settle_task)
        self._pending_worker_lease_task = task
        self._pending_worker_lease_delivery = delivery
        self._pending_worker_lease_epoch = self._worker_config_epoch
        return task, delivery, self._pending_worker_lease_epoch

    async def install_committed_worker_generation(self) -> None:
        """Install the committed worker configuration for subsequent launches."""
        self._worker_config_epoch += 1
        try:
            await asyncio.wait_for(self._refresh_worker_backend(), timeout=self.pass_timeout_seconds)
        except TimeoutError:
            self._clear_current_worker_backend()
            logger.warning(
                "script_worker_backend_commit_refresh_timeout",
                timeout_seconds=self.pass_timeout_seconds,
            )
        except WorkerBackendError:
            self._clear_current_worker_backend()
            logger.warning("script_worker_backend_commit_refresh_pending", exc_info=True)

    def _clear_current_worker_backend(self) -> None:
        self._current_worker_lease = None
        self.manager.worker_backend = None
        self.manager.worker_backend_generation = None

    def _worker_backend_for(self, run: ScriptRunRecord | None) -> WorkerBackend | None:
        binding = self._worker_backend_binding_for(run)
        return None if binding is None else binding.backend

    def _worker_backend_binding_for(self, run: ScriptRunRecord | None) -> ScriptWorkerBackendBinding | None:
        """Return the exact leased backend generation assigned to one durable run."""
        current = self._current_worker_lease
        if run is None:
            if current is None:
                return None
            return ScriptWorkerBackendBinding(current.manager, current.generation_id)
        generation_id = run.worker_backend_generation
        if generation_id is None:
            return None
        for lease in self._worker_leases:
            if lease.generation_id == generation_id:
                return ScriptWorkerBackendBinding(lease.manager, lease.generation_id)
        return None

    async def _release_unused_worker_leases(self, runs: list[ScriptRunRecord]) -> None:
        current = self._current_worker_lease
        retained: list[_WorkerManagerLease] = []
        unresolved_assignment = any(not run.local_unsafe and run.worker_backend_generation is None for run in runs)
        live_generations = {run.worker_backend_generation for run in runs if run.worker_backend_generation is not None}
        released: list[_WorkerManagerLease] = []
        for lease in self._worker_leases:
            if lease is current or unresolved_assignment:
                retained.append(lease)
                continue
            if lease.generation_id in live_generations:
                retained.append(lease)
                continue
            released.append(lease)
        self._worker_leases = retained
        await asyncio.gather(*(asyncio.to_thread(lease.release) for lease in released))

    async def apply_update_plan(self, plan: ConfigUpdatePlan) -> None:
        """Revoke removed owners and process-isolation changes before bot replacement."""
        current_config = self.config_provider()
        if current_config is None:
            return
        removed_agents = plan.removed_entities & set(current_config.agents)
        isolation_changes = {
            agent_name
            for agent_name in set(current_config.agents) & set(plan.new_config.agents)
            if _agent_isolation_changed(current_config.agents[agent_name], plan.new_config.agents[agent_name])
        }
        if not removed_agents and not isolation_changes:
            return
        try:
            await asyncio.wait_for(
                self._apply_update_pass(
                    removed_agents=removed_agents,
                    isolation_changes=isolation_changes,
                ),
                timeout=self.pass_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("script_reload_reconciliation_timeout", timeout_seconds=self.pass_timeout_seconds)

    async def _apply_update_pass(
        self,
        *,
        removed_agents: set[str],
        isolation_changes: set[str],
    ) -> None:
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        affected = [run for run in runs if run.agent_name in removed_agents | isolation_changes]
        semaphore = asyncio.Semaphore(self.pass_concurrency)

        def reason_for(run: ScriptRunRecord) -> str:
            return _REMOVED_AGENT_REASON if run.agent_name in removed_agents else _ISOLATION_CHANGE_REASON

        async def persist_revocation(run: ScriptRunRecord) -> bool:
            async with semaphore:
                try:
                    await asyncio.to_thread(
                        self.manager.request_revocation,
                        run_id=run.run_id,
                        reason=reason_for(run),
                    )
                except (ScriptRunManagerError, ScriptRunStoreError):
                    logger.warning(
                        "script_reload_durable_revocation_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=True,
                    )
                    return False
                return True

        durable_results = await asyncio.gather(*(persist_revocation(run) for run in affected))
        durably_revoked = [run for run, persisted in zip(affected, durable_results, strict=True) if persisted]

        async def revoke_broker_ownership(run: ScriptRunRecord) -> bool:
            async with semaphore:
                try:
                    await self.manager.revoke(run.run_id, reason=reason_for(run))
                except (
                    ScriptRunManagerError,
                    ScriptWorkerError,
                    WorkerBackendError,
                    _ScriptRuntimeUnavailableError,
                ):
                    logger.warning(
                        "script_reload_broker_revocation_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=True,
                    )
                    return False
                return True

        broker_results = await asyncio.gather(
            *(revoke_broker_ownership(run) for run in durably_revoked),
        )
        broker_revoked = [run for run, revoked in zip(durably_revoked, broker_results, strict=True) if revoked]

        async def reconcile_run(run: ScriptRunRecord) -> None:
            async with semaphore:
                try:
                    await self.manager.reconcile_durable(run_id=run.run_id, broker_revoked=True)
                except (
                    ScriptRunManagerError,
                    ScriptWorkerError,
                    WorkerBackendError,
                    _ScriptRuntimeUnavailableError,
                ):
                    logger.warning(
                        "script_reload_reconciliation_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=True,
                    )

        await asyncio.gather(*(reconcile_run(run) for run in broker_revoked))
        unfinished = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        await self._release_unused_worker_leases(unfinished)

    async def reconcile_once(self) -> None:  # privata: ignore -- explicit lifecycle sweep API.
        """Run one bounded touch-first reconciliation pass."""
        try:
            await asyncio.wait_for(self._reconcile_pass(), timeout=self.pass_timeout_seconds)
        except TimeoutError:
            logger.warning("script_reconciliation_pass_timeout", timeout_seconds=self.pass_timeout_seconds)

    async def _reconcile_pass(self) -> None:
        try:
            await self._refresh_worker_backend()
        except WorkerBackendError:
            logger.warning("script_worker_backend_refresh_pending", exc_info=True)
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        touch_targets: dict[tuple[int, str], tuple[WorkerBackend, str]] = {}
        for run in runs:
            run_backend = self._worker_backend_for(run)
            if run_backend is not None and run.worker_key is not None:
                touch_targets[(id(run_backend), run.worker_key)] = (run_backend, run.worker_key)
        await asyncio.gather(
            *(self._touch_worker(run_backend, worker_key) for run_backend, worker_key in touch_targets.values()),
        )
        semaphore = asyncio.Semaphore(self.pass_concurrency)

        async def reconcile_run(run: ScriptRunRecord) -> None:
            async with semaphore:
                try:
                    await self.manager.reconcile_durable(run_id=run.run_id)
                except (ScriptRunManagerError, ScriptWorkerError, WorkerBackendError):
                    logger.warning(
                        "script_run_reconciliation_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=True,
                    )

        await asyncio.gather(*(reconcile_run(run) for run in runs))
        unfinished = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        await self._release_unused_worker_leases(unfinished)

    async def _touch_worker(self, backend: WorkerBackend, worker_key: str) -> None:
        try:
            await asyncio.to_thread(backend.touch_worker, worker_key)
        except WorkerBackendError:
            logger.warning("script_worker_touch_pending", worker_key=worker_key, exc_info=True)

    def touch_live_workers(self, backend: WorkerBackend) -> None:
        """Refresh active-run worker leases immediately before idle cleanup."""
        for worker_key in sorted(
            {run.worker_key for run in self.store.list_runs(include_finished=False) if run.worker_key is not None},
        ):
            try:
                backend.touch_worker(worker_key)
            except WorkerBackendError:
                logger.warning(
                    "script_worker_touch_pending",
                    worker_key=worker_key,
                    exc_info=True,
                )

    async def prune_once(  # privata: ignore -- deterministic retention sweep API.
        self,
        *,
        now: datetime | None = None,
    ) -> None:
        """Prune terminal snapshots, receipts, and run rows after retention."""
        try:
            await asyncio.wait_for(self._prune_pass(now=now), timeout=self.pass_timeout_seconds)
        except TimeoutError:
            logger.warning("script_retention_pass_timeout", timeout_seconds=self.pass_timeout_seconds)

    async def _prune_pass(self, *, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=self.retention_seconds)
        finished_before = cutoff.isoformat().replace("+00:00", "Z")
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=True)
        for run in runs:
            if (
                run.state
                not in {
                    ScriptRunState.EXITED,
                    ScriptRunState.FAILED,
                    ScriptRunState.CANCELLED,
                    ScriptRunState.INTERRUPTED,
                }
                or run.finished_at is None
            ):
                continue
            if run.finished_at > finished_before:
                continue
            try:
                approvals_pruned = await self.resolver.prune_approvals(run.run_id)
                if not approvals_pruned:
                    continue
                cleaned = await self.manager.cleanup_snapshot(run)
                if cleaned is False:
                    continue
                await asyncio.to_thread(
                    self.store.prune_terminal_run,
                    run.run_id,
                    finished_before=finished_before,
                )
            except (ScriptRunManagerError, _ScriptRuntimeUnavailableError):
                logger.warning(
                    "script_run_retention_pending",
                    run_id=run.run_id,
                    agent_name=run.agent_name,
                    exc_info=True,
                )

    async def _complete_pass(self) -> None:
        await self._reconcile_pass()
        await self._prune_pass()

    async def _run_complete_pass(self, *, timeout_event: str) -> None:
        try:
            await asyncio.wait_for(self._complete_pass(), timeout=self.pass_timeout_seconds)
        except TimeoutError:
            logger.warning(timeout_event, timeout_seconds=self.pass_timeout_seconds)

    async def shutdown(self, *, timeout_seconds: float = 5.0) -> None:
        """Run bounded final reconciliation, then clear the process-local tool binding."""
        shutdown_deadline = asyncio.get_running_loop().time() + timeout_seconds
        self._start_requested = False
        startup_task, self._startup_task = self._startup_task, None
        await cancel_task(startup_task)
        maintenance_task, self._maintenance_task = self._maintenance_task, None
        await cancel_task(maintenance_task)
        if not self._activated_once:
            return

        async def _cleanup() -> None:
            await self._complete_pass()

        try:
            try:
                await asyncio.wait_for(_cleanup(), timeout=timeout_seconds)
            except TimeoutError:
                logger.warning("script_shutdown_reconciliation_timeout", timeout_seconds=timeout_seconds)
            cleanup_drained = await drain_script_tool_cleanup(
                self.broker,
                timeout_seconds=max(0.0, shutdown_deadline - asyncio.get_running_loop().time()),
            )
            if not cleanup_drained:
                logger.warning("script_shutdown_tool_cleanup_timeout", timeout_seconds=timeout_seconds)
        finally:
            bind_script_run_manager(None)
            self._started = False
            self._activated_once = False
            leases = list(self._worker_leases)
            pending_lease_task = self._pending_worker_lease_task
            pending_delivery = self._pending_worker_lease_delivery
            if pending_delivery is not None:
                pending_lease = pending_delivery.abandon()
                if pending_lease is not None:
                    leases.append(pending_lease)
            if pending_lease_task is not None and pending_lease_task.done() and not pending_lease_task.cancelled():
                pending_failure = pending_lease_task.exception()
                if pending_failure is not None:
                    logger.warning(
                        "script_worker_backend_pending_acquire_failed",
                        exc_info=(type(pending_failure), pending_failure, pending_failure.__traceback__),
                    )
            self._pending_worker_lease_task = None
            self._pending_worker_lease_delivery = None
            self._pending_worker_lease_epoch = -1
            self._worker_leases.clear()
            self._current_worker_lease = None
            self.manager.worker_backend = None
            self.manager.worker_backend_generation = None
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(asyncio.to_thread(lease.release) for lease in leases)),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                logger.warning("script_worker_backend_release_timeout", timeout_seconds=timeout_seconds)


def _release_worker_lease_later(lease: _WorkerManagerLease) -> None:
    """Release an acknowledged-cancelled delivery without blocking the event loop."""
    create_logged_task(
        asyncio.to_thread(lease.release),
        name="script_worker_backend_late_release",
        failure_message="Late background script worker lease release failed",
    )


def build_script_runtime(
    runtime_paths: RuntimePaths,
    *,
    config_provider: Callable[[], Config | None],
    bot_provider: Callable[[str], AgentBot | None],
    worker_lease_provider: Callable[[], _WorkerManagerLease | None],
    api_enabled: bool,
) -> ScriptRuntimeLifecycle:
    """Construct the one process-local script store, resolver, broker, and manager."""
    store = ScriptRunStore(runtime_paths)
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=runtime_paths,
        bot_provider=bot_provider,
        worker_backend_provider=lambda _run: None,
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="",
    )
    retention_seconds = _script_retention_seconds(runtime_paths)
    lifecycle = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=config_provider,
        worker_lease_provider=worker_lease_provider,
        api_enabled=api_enabled,
        retention_seconds=retention_seconds,
    )
    resolver.worker_backend_provider = lifecycle._worker_backend_for
    manager.worker_backend_resolver = lifecycle._worker_backend_binding_for
    return lifecycle


def _agent_isolation_changed(current: AgentConfig, updated: AgentConfig) -> bool:
    """Return whether one agent's process isolation contract changed."""
    return current.private != updated.private


def _script_retention_seconds(runtime_paths: RuntimePaths) -> float:
    raw = (
        runtime_paths.env_value(
            _SCRIPT_RETENTION_SECONDS_ENV,
            default=str(_DEFAULT_SCRIPT_RETENTION_SECONDS),
        )
        or ""
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        msg = f"{_SCRIPT_RETENTION_SECONDS_ENV} must be a positive number"
        raise ValueError(msg) from None
    if not math.isfinite(value) or value <= 0:
        msg = f"{_SCRIPT_RETENTION_SECONDS_ENV} must be a positive number"
        raise ValueError(msg)
    return value


def script_gateway_url(runtime_paths: RuntimePaths, *, host: str, port: int) -> str:
    """Return the gateway URL injected into isolated script processes."""
    worker_process_enabled = script_execution_uses_worker(runtime_paths) or primary_worker_backend_is_dedicated(
        runtime_paths,
    )
    explicit_url = (runtime_paths.env_value("MINDROOM_SCRIPT_GATEWAY_URL") or "").strip()
    if explicit_url:
        gateway_url = explicit_url.rstrip("/")
        _validate_script_gateway(gateway_url, worker_process_enabled=worker_process_enabled)
        return gateway_url
    public_url = (runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").strip()
    if public_url:
        gateway_url = f"{public_url.rstrip('/')}/api/script-gateway"
        _validate_script_gateway(gateway_url, worker_process_enabled=worker_process_enabled)
        return gateway_url
    if worker_process_enabled:
        msg = "Background-script workers require MINDROOM_SCRIPT_GATEWAY_URL or MINDROOM_PUBLIC_URL."
        raise ValueError(msg)
    gateway_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host  # noqa: S104
    return f"http://{gateway_host}:{port}/api/script-gateway"


def _validate_script_gateway(gateway_url: str, *, worker_process_enabled: bool) -> None:
    parsed = urlsplit(gateway_url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is None and ":" in parsed.netloc.rsplit("]", maxsplit=1)[-1])
    ):
        msg = "Background-script gateway must be a valid HTTP(S) URL."
        raise ValueError(msg)
    if not worker_process_enabled:
        return
    try:
        resolved = socket.getaddrinfo(
            parsed.hostname,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = {
            ipaddress.ip_address(str(sockaddr[0]).partition("%")[0])
            for _family, _type, _protocol, _canonical_name, sockaddr in resolved
        }
    except (OSError, ValueError):
        addresses = set()
    if not addresses or any(address.is_loopback or address.is_unspecified for address in addresses):
        msg = "Background-script workers require a non-loopback gateway URL."
        raise ValueError(msg)
