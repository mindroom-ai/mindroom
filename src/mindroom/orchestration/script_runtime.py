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
from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.custom_tools.script import bind_script_run_manager
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker, drain_script_tool_cleanup
from mindroom.script_runs.manager import (
    ScriptRunManager,
    ScriptRunManagerError,
    script_execution_uses_worker,
)
from mindroom.script_runs.models import (
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
)
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
from mindroom.workers.runtime import (
    configured_primary_worker_manager_identity,
    primary_worker_backend_is_dedicated,
)

from .runtime import cancel_task, create_logged_task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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
_SCRIPT_TOOL_REMOVED_REASON = "Background script tool was removed by configuration reload."
_WORKER_CONFIGURATION_INTERRUPTION_REASON = "Worker configuration changed during configuration reload."
_RUNTIME_SHUTDOWN_INTERRUPTION_REASON = "MindRoom runtime shut down."
_RUNTIME_STARTUP_INTERRUPTION_REASON = "MindRoom runtime restarted."
_AUTHORIZATION_REVOKED_REASON = "Script owner no longer has room-and-agent reply authorization."
_SCRIPT_RETENTION_SECONDS_ENV = "MINDROOM_SCRIPT_RETENTION_SECONDS"
_DEFAULT_SCRIPT_RETENTION_SECONDS = 30 * 24 * 60 * 60


class _ScriptRuntimeUnavailableError(RuntimeError):
    """The durable script owner has no live runtime generation yet."""


class _ScriptRuntimeLifecycleError(RuntimeError):
    """A configuration update cannot safely cross the script runtime boundary."""


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

    def is_authorized(self, run: ScriptRunRecord, *, config: Config | None = None) -> bool:
        """Check the durable owner against current room, reply, and membership authority."""
        bot = self.bot_provider(run.agent_name)
        if bot is None or not bot.running:
            return False
        current_config = bot.config if config is None else config
        return is_sender_allowed_for_agent_reply_in_room(
            run.owner_user_id,
            run.agent_name,
            current_config,
            run.room_id,
            self.runtime_paths,
            bot._runtime_view.agent_reply_memberships,
        )

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
    """Keep one broker/manager pair stable across runtime configuration updates."""

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
    _current_worker_lease: _WorkerManagerLease | None = field(default=None, init=False, repr=False)
    _worker_config_epoch: int = field(default=0, init=False, repr=False)
    _pending_worker_lease_task: asyncio.Task[_WorkerManagerLease | None] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _pending_worker_lease_delivery: _WorkerLeaseDelivery | None = field(default=None, init=False, repr=False)
    _pending_worker_lease_epoch: int = field(default=-1, init=False, repr=False)
    _startup_cleanup_pending: bool = field(default=False, init=False, repr=False)

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
            await self.manager.begin_startup_reconciliation()
            bind_script_run_manager(self.manager)
            self._started = True
            self._activated_once = True
            self._startup_cleanup_pending = True
            try:
                await asyncio.wait_for(
                    self._startup_cleanup_pass(),
                    timeout=self.pass_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("script_startup_pass_timeout", timeout_seconds=self.pass_timeout_seconds)
            except Exception:
                logger.warning("script_startup_cleanup_pending", exc_info=True)
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
        current = self._current_worker_lease
        if current is not None:
            self.manager.worker_backend = current.manager
            return current.manager
        lease = await self._acquire_current_worker_lease()
        if lease is None:
            self._clear_current_worker_backend()
            return None
        self._current_worker_lease = lease
        backend = lease.manager
        self.manager.worker_backend = backend
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

    async def complete_worker_replacement(self) -> None:
        """Reopen safely, then publish the backend for the config visible now."""
        if not self.manager.worker_replacement_in_progress:
            return

        retired_lease = self._current_worker_lease
        self._clear_current_worker_backend()
        self._worker_config_epoch += 1
        release_task = None if retired_lease is None else _release_worker_lease_later(retired_lease)
        await run_coroutine_until_complete(self.manager.end_worker_replacement())
        if release_task is not None:
            await asyncio.shield(release_task)

        async with self._worker_refresh_lock:
            try:
                if self.config_provider() is None:
                    self._clear_current_worker_backend()
                else:
                    await asyncio.wait_for(
                        self._refresh_worker_backend_locked(),
                        timeout=self.pass_timeout_seconds,
                    )
            except TimeoutError:
                self._clear_current_worker_backend()
                logger.warning(
                    "script_worker_backend_commit_refresh_timeout",
                    timeout_seconds=self.pass_timeout_seconds,
                )
            except Exception:
                self._clear_current_worker_backend()
                logger.warning("script_worker_backend_commit_refresh_pending", exc_info=True)

    def _clear_current_worker_backend(self) -> None:
        self._current_worker_lease = None
        self.manager.worker_backend = None

    def _worker_backend_for(self, run: ScriptRunRecord | None) -> WorkerBackend | None:
        """Resolve every active worker-backed run through the current lease."""
        del run
        current = self._current_worker_lease
        return None if current is None else current.manager

    async def _release_current_worker_lease(self) -> None:
        lease = self._current_worker_lease
        self._clear_current_worker_backend()
        if lease is not None:
            await asyncio.to_thread(lease.release)

    async def apply_update_plan(self, plan: ConfigUpdatePlan) -> None:
        """Revoke removed owners and process-isolation changes before bot replacement."""
        current_config = self.config_provider()
        if current_config is None:
            return
        current_worker_configuration_identity = configured_primary_worker_manager_identity(
            self.runtime_paths,
            current_config,
        )
        next_worker_configuration_identity = configured_primary_worker_manager_identity(
            self.runtime_paths,
            plan.new_config,
        )
        worker_configuration_changed = current_worker_configuration_identity != next_worker_configuration_identity
        removed_agents = plan.removed_entities & set(current_config.agents)
        isolation_changes = {
            agent_name
            for agent_name in set(current_config.agents) & set(plan.new_config.agents)
            if _agent_isolation_changed(current_config.agents[agent_name], plan.new_config.agents[agent_name])
        }
        script_tool_removals = {
            agent_name
            for agent_name in set(current_config.agents) & set(plan.new_config.agents)
            if _agent_has_script_tool(current_config, agent_name)
            and not _agent_has_script_tool(plan.new_config, agent_name)
        }
        authorization_changed = current_config.authorization != plan.new_config.authorization
        if (
            not removed_agents
            and not isolation_changes
            and not script_tool_removals
            and not worker_configuration_changed
            and not authorization_changed
        ):
            return

        replacement_started = False

        async def apply_update_boundary() -> None:
            nonlocal replacement_started
            if worker_configuration_changed:
                replacement_started = True
                await self.manager.begin_worker_replacement()
            await self._apply_update_pass(
                removed_agents=removed_agents,
                isolation_changes=isolation_changes,
                script_tool_removals=script_tool_removals,
                worker_configuration_changed=worker_configuration_changed,
                authorization_config=plan.new_config if authorization_changed else None,
            )

        try:
            await asyncio.wait_for(
                apply_update_boundary(),
                timeout=self.pass_timeout_seconds,
            )
        except BaseException as exc:
            if replacement_started:
                await run_coroutine_until_complete(self.manager.end_worker_replacement())
            if isinstance(exc, TimeoutError):
                msg = "Background script reload did not durably revoke every active run before the reload deadline."
                raise _ScriptRuntimeLifecycleError(msg) from None
            raise

    async def _apply_update_pass(
        self,
        *,
        removed_agents: set[str],
        isolation_changes: set[str],
        script_tool_removals: set[str],
        worker_configuration_changed: bool,
        authorization_config: Config | None,
    ) -> None:
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        unauthorized_ids = {
            run.run_id
            for run in runs
            if authorization_config is not None and not self.resolver.is_authorized(run, config=authorization_config)
        }
        affected_agents = removed_agents | isolation_changes | script_tool_removals
        affected = [
            run
            for run in runs
            if run.run_id in unauthorized_ids
            or run.agent_name in affected_agents
            or (worker_configuration_changed and not run.local_unsafe)
        ]
        await self._interrupt_runs(
            affected,
            reason_for=lambda run: (
                _AUTHORIZATION_REVOKED_REASON
                if run.run_id in unauthorized_ids
                else _reload_reason_for(
                    run,
                    removed_agents=removed_agents,
                    isolation_changes=isolation_changes,
                    worker_configuration_changed=worker_configuration_changed,
                )
            ),
            require_worker_success=worker_configuration_changed,
        )

        unfinished = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        _require_terminal_worker_replacement(
            unfinished,
            worker_configuration_changed=worker_configuration_changed,
        )
        if worker_configuration_changed:
            await self._release_current_worker_lease()

    async def _interrupt_runs(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        reason_for: Callable[[ScriptRunRecord], str],
        require_worker_success: bool,
    ) -> None:
        """Durably revoke, close broker work, and reconcile selected processes in phases."""
        durably_revoked = await self._durably_revoke_runs(runs, reason_for=reason_for)
        await self._interrupt_durably_revoked_runs(
            durably_revoked,
            reason_for=reason_for,
            require_worker_success=require_worker_success,
        )

    async def _durably_revoke_runs(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        reason_for: Callable[[ScriptRunRecord], str],
    ) -> Sequence[ScriptRunRecord]:
        """Persist cancellation intent for every selected run before later cleanup."""
        semaphore = asyncio.Semaphore(self.pass_concurrency)

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

        durable_results = await asyncio.gather(*(persist_revocation(run) for run in runs))
        if not all(durable_results):
            msg = "Worker replacement did not durably revoke every active run."
            raise _ScriptRuntimeLifecycleError(msg)
        return [run for run, persisted in zip(runs, durable_results, strict=True) if persisted]

    async def _interrupt_durably_revoked_runs(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        reason_for: Callable[[ScriptRunRecord], str],
        require_worker_success: bool,
    ) -> None:
        """Close broker work and reconcile processes after durable revocation."""
        semaphore = asyncio.Semaphore(self.pass_concurrency)

        async def revoke_broker_ownership(run: ScriptRunRecord) -> bool:
            async with semaphore:
                try:
                    await self.manager.revoke(
                        run.run_id,
                        reason=reason_for(run),
                    )
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
            *(revoke_broker_ownership(run) for run in runs),
        )
        broker_revoked = [run for run, revoked in zip(runs, broker_results, strict=True) if revoked]
        _require_successful_worker_replacement_stage(
            runs,
            broker_results,
            worker_configuration_changed=require_worker_success,
            error="Worker replacement did not close broker ownership for every active worker run.",
        )

        async def reconcile_run(run: ScriptRunRecord) -> bool:
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
                    return False
                return True

        reconciliation_results = await asyncio.gather(*(reconcile_run(run) for run in broker_revoked))
        _require_successful_worker_replacement_stage(
            broker_revoked,
            reconciliation_results,
            worker_configuration_changed=require_worker_success,
            error="Worker replacement did not complete process reconciliation for every active worker run.",
        )

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
        unauthorized = [run for run in runs if not self.resolver.is_authorized(run)]
        if unauthorized:
            await self._interrupt_runs(
                unauthorized,
                reason_for=lambda _run: _AUTHORIZATION_REVOKED_REASON,
                require_worker_success=False,
            )
            unauthorized_ids = {run.run_id for run in unauthorized}
            runs = [run for run in runs if run.run_id not in unauthorized_ids]
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

    async def _startup_cleanup_pass(self) -> None:
        """Revoke and retire every inherited nonterminal run before reopening launches."""
        try:
            await self._refresh_worker_backend()
        except WorkerBackendError:
            logger.warning("script_worker_backend_refresh_pending", exc_info=True)
        inherited = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        if inherited:
            await self._interrupt_runs(
                inherited,
                reason_for=lambda _run: _RUNTIME_STARTUP_INTERRUPTION_REASON,
                require_worker_success=False,
            )
        unfinished = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        if unfinished:
            return
        await self.manager.end_startup_reconciliation()
        self._startup_cleanup_pending = False

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
        """Prune terminal approvals, receipts, and run rows after retention."""
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
                await asyncio.to_thread(
                    self.store.prune_terminal_run,
                    run.run_id,
                    finished_before=finished_before,
                )
            except (ScriptRunManagerError, WorkerBackendError, _ScriptRuntimeUnavailableError):
                logger.warning(
                    "script_run_retention_pending",
                    run_id=run.run_id,
                    agent_name=run.agent_name,
                    exc_info=True,
                )

    async def _complete_pass(self) -> None:
        if self._startup_cleanup_pending:
            await self._startup_cleanup_pass()
            if self._startup_cleanup_pending:
                return
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
        was_activated = self._activated_once
        self._start_requested = False
        startup_task, self._startup_task = self._startup_task, None
        await cancel_task(startup_task)
        maintenance_task, self._maintenance_task = self._maintenance_task, None
        await cancel_task(maintenance_task)

        await run_coroutine_until_complete(self.manager.begin_shutdown())
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        durably_revoked = await run_coroutine_until_complete(
            self._durably_revoke_runs(
                runs,
                reason_for=lambda _run: _RUNTIME_SHUTDOWN_INTERRUPTION_REASON,
            ),
        )
        try:
            await self._run_shutdown_cleanup(
                durably_revoked,
                deadline=shutdown_deadline,
                timeout_seconds=timeout_seconds,
            )
        finally:
            if was_activated:
                bind_script_run_manager(None)
            self._started = False
            self._activated_once = False
            await _release_worker_leases_before_deadline(
                self._detach_worker_leases(),
                deadline=shutdown_deadline,
                timeout_seconds=timeout_seconds,
            )

    async def _run_shutdown_cleanup(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        deadline: float,
        timeout_seconds: float,
    ) -> None:
        try:
            await asyncio.wait_for(
                self._interrupt_and_prune_for_shutdown(runs),
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except TimeoutError:
            logger.warning("script_shutdown_reconciliation_timeout", timeout_seconds=timeout_seconds)
        cleanup_drained = await drain_script_tool_cleanup(
            self.broker,
            timeout_seconds=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
        if not cleanup_drained:
            logger.warning("script_shutdown_tool_cleanup_timeout", timeout_seconds=timeout_seconds)

    async def _interrupt_and_prune_for_shutdown(self, runs: Sequence[ScriptRunRecord]) -> None:
        await self._interrupt_durably_revoked_runs(
            runs,
            reason_for=lambda _run: _RUNTIME_SHUTDOWN_INTERRUPTION_REASON,
            require_worker_success=False,
        )
        await self._prune_pass()

    def _detach_worker_leases(self) -> list[_WorkerManagerLease]:
        leases = [self._current_worker_lease] if self._current_worker_lease is not None else []
        pending_lease_task = self._pending_worker_lease_task
        if self._pending_worker_lease_delivery is not None:
            pending_lease = self._pending_worker_lease_delivery.abandon()
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
        self._current_worker_lease = None
        self.manager.worker_backend = None
        return leases


async def _release_worker_leases_before_deadline(
    leases: list[_WorkerManagerLease],
    *,
    deadline: float,
    timeout_seconds: float,
) -> None:
    release_tasks = [_release_worker_lease_later(lease) for lease in leases]
    if not release_tasks:
        return
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    _done, pending = await asyncio.wait(release_tasks, timeout=remaining)
    if pending:
        logger.warning("script_worker_backend_release_timeout", timeout_seconds=timeout_seconds)


def _release_worker_lease_later(lease: _WorkerManagerLease) -> asyncio.Task[None]:
    """Release an acknowledged-cancelled delivery without blocking the event loop."""
    return create_logged_task(
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
    return lifecycle


def _agent_isolation_changed(current: AgentConfig, updated: AgentConfig) -> bool:
    """Return whether one agent's process isolation contract changed."""
    return current.private != updated.private


def _agent_has_script_tool(config: Config, agent_name: str) -> bool:
    """Return whether one agent's effective tool surface includes script controls."""
    return any(entry.name == "script" for entry in config.resolve_entity(agent_name).tool_configs)


def _require_successful_worker_replacement_stage(
    runs: Sequence[ScriptRunRecord],
    results: Sequence[bool],
    *,
    worker_configuration_changed: bool,
    error: str,
) -> None:
    worker_run_failed = any(
        not succeeded and not run.local_unsafe for run, succeeded in zip(runs, results, strict=True)
    )
    if worker_configuration_changed and worker_run_failed:
        raise _ScriptRuntimeLifecycleError(error)


def _require_terminal_worker_replacement(
    unfinished: Sequence[ScriptRunRecord],
    *,
    worker_configuration_changed: bool,
) -> None:
    if worker_configuration_changed and any(not run.local_unsafe for run in unfinished):
        msg = "Worker replacement did not publish terminal durable state for every active worker run."
        raise _ScriptRuntimeLifecycleError(msg)


def _reload_reason_for(
    run: ScriptRunRecord,
    *,
    removed_agents: set[str],
    isolation_changes: set[str],
    worker_configuration_changed: bool,
) -> str:
    if worker_configuration_changed and not run.local_unsafe:
        return _WORKER_CONFIGURATION_INTERRUPTION_REASON
    if run.agent_name in removed_agents:
        return _REMOVED_AGENT_REASON
    if run.agent_name in isolation_changes:
        return _ISOLATION_CHANGE_REASON
    return _SCRIPT_TOOL_REMOVED_REASON


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


async def script_gateway_url(runtime_paths: RuntimePaths, *, host: str, port: int) -> str:
    """Return the gateway URL injected into isolated script processes."""
    worker_process_enabled = script_execution_uses_worker(
        runtime_paths,
        worker_backend_configured=primary_worker_backend_is_dedicated(runtime_paths),
    )
    if not worker_process_enabled:
        gateway_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host  # noqa: S104
        return f"http://{gateway_host}:{port}/api/script-gateway"
    explicit_url = (runtime_paths.env_value("MINDROOM_SCRIPT_GATEWAY_URL") or "").strip()
    if explicit_url:
        gateway_url = explicit_url.rstrip("/")
        await _validate_script_gateway(gateway_url, worker_process_enabled=worker_process_enabled)
        return gateway_url
    public_url = (runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").strip()
    if public_url:
        gateway_url = f"{public_url.rstrip('/')}/api/script-gateway"
        await _validate_script_gateway(gateway_url, worker_process_enabled=worker_process_enabled)
        return gateway_url
    msg = "Background-script workers require MINDROOM_SCRIPT_GATEWAY_URL or MINDROOM_PUBLIC_URL."
    raise ValueError(msg)


async def _validate_script_gateway(gateway_url: str, *, worker_process_enabled: bool) -> None:
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
        or bool(parsed.query)
        or bool(parsed.fragment)
        or (port is None and ":" in parsed.netloc.rsplit("]", maxsplit=1)[-1])
    ):
        msg = "Background-script gateway must be a valid HTTP(S) URL."
        raise ValueError(msg)
    if not worker_process_enabled:
        return
    try:
        resolved = await asyncio.to_thread(
            socket.getaddrinfo,
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
