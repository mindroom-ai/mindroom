"""Focused lifecycle ownership for the primary background-script runtime."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

from mindroom.approval import manager as approval_manager
from mindroom.approval.tool_approval import (
    BackgroundScriptToolOrigin,
    ToolApprovalDecision,
    human_gate,
    resolve_tool_approval_approver,
)
from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.origins import configured_public_url
from mindroom.response_activity import ActiveScriptRunInfo
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker, drain_script_tool_cleanup
from mindroom.script_runs.manager import (
    ScriptRunManager,
    ScriptRunManagerError,
    script_execution_uses_worker,
    script_gateway_isolated,
)
from mindroom.script_runs.models import (
    TERMINAL_SCRIPT_RUN_STATES,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
    utc_timestamp,
)
from mindroom.script_runs.reasons import (
    AGENT_ISOLATION_CHANGED,
    OWNER_AGENT_REMOVED,
    OWNER_AUTHORIZATION_REVOKED,
    PLUGIN_TOOLS_CHANGED,
    RUNTIME_RESTARTED,
    RUNTIME_SHUTDOWN,
    SCRIPT_TOOL_REMOVED,
    WORKER_CONFIGURATION_CHANGED,
)
from mindroom.script_runs.recovery import run_matches_recovery_signature, script_process_authority
from mindroom.script_runs.store import ScriptRunStore, ScriptRunStoreError
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.tool_system.worker_routing import (
    parse_tool_execution_identity_payload,
    worker_log_id,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import worker_backend_kind

from .runtime import cancel_task, create_logged_task

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import BackgroundApprovalDecision
    from mindroom.orchestration.config_updates import ConfigUpdatePlan
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.backend import WorkerBackend

logger = get_logger(__name__)

_SCRIPT_RETENTION_SECONDS_ENV = "MINDROOM_SCRIPT_RETENTION_SECONDS"
_DEFAULT_SCRIPT_RETENTION_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class _ScriptRuntimeUnavailableError(RuntimeError):
    """The durable script owner has no live runtime generation yet."""


class _ScriptRuntimeLifecycleError(RuntimeError):
    """A configuration update cannot safely cross the script runtime boundary."""


class _BackgroundApprovalManager(Protocol):
    async def request_background_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        owner_kind: Literal["job", "script"],
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

    def release(self) -> None: ...


@dataclass(slots=True)
class _WorkerLeaseDelivery:
    """Own a provider lease until the asyncio consumer acknowledges delivery."""

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _lease: _WorkerManagerLease | None = field(default=None, init=False, repr=False)
    _abandoned: bool = field(default=False, init=False, repr=False)

    def acquire(
        self,
        provider: Callable[[str | None], _WorkerManagerLease | None],
        required_backend_locator: str | None,
    ) -> _WorkerManagerLease | None:
        """Acquire in the executor and release there if delivery was abandoned."""
        lease = provider(required_backend_locator)
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
                exc_info=failure,
            )


@dataclass(frozen=True, slots=True)
class _PendingWorkerLease:
    """Keep one acquisition task and its delivery ownership together."""

    task: asyncio.Task[_WorkerManagerLease | None]
    delivery: _WorkerLeaseDelivery
    backend_locator: str | None


@dataclass(slots=True)
class _LiveScriptRuntimeResolver:
    """Rebuild current runtime, worker, and approval authority for a durable run."""

    runtime_paths: RuntimePaths
    bot_provider: Callable[[str], AgentBot | None]
    worker_backend_provider: Callable[[ScriptRunRecord], WorkerBackend | None]
    approval_provider: Callable[[], _BackgroundApprovalManager | None] = approval_manager.get_approval_store

    def is_authorized(self, run: ScriptRunRecord, *, config: Config | None = None) -> bool | None:
        """Return confirmed authority, denial, or transient live-runtime unavailability."""
        bot = self.bot_provider(run.agent_name)
        if bot is None or not bot.running:
            return None
        current_config = bot.config if config is None else config
        return is_sender_allowed_for_agent_reply_in_room(
            run.owner_user_id,
            run.agent_name,
            current_config,
            run.room_id,
            self.runtime_paths,
            bot.orchestrator.agent_reply_memberships,
        )

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        """Rebuild a context only from the exact durable Matrix execution identity."""
        identity = _run_execution_identity(run)
        if (
            identity.channel != "matrix"
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
        context = bot.tool_runtime_support.build_context(
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
        worker_target = resolve_agent_execution(
            context.agent_name,
            context.current_config,
            context.runtime_paths,
            execution_identity=_run_execution_identity(run),
        ).worker_target
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
        """Await one exact-call decision in the existing Matrix approval domain, after the broker's policy gated it."""
        approver_user_id = resolve_tool_approval_approver(
            context.current_config,
            context.runtime_paths,
            context.requester_id,
        )
        gate = human_gate(
            context.runtime_paths,
            approver_id=approver_user_id,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
        )
        if gate is not None:
            return gate
        assert approver_user_id is not None, "the gate denies a call no one may approve"
        approvals = self.approval_provider()
        if approvals is None:
            return ToolApprovalDecision(approved=False, reason=approval_manager.RUNTIME_NOT_READY_REASON)
        decision = await approvals.request_background_approval(
            origin=origin,
            owner_kind="script",
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
            msg = approval_manager.RUNTIME_NOT_READY_REASON
            raise _ScriptRuntimeUnavailableError(msg)
        await approvals.settle_background_approval(origin, reason=reason)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        """Retire every pending approval whose broker run ownership ended."""
        approvals = self.approval_provider()
        if approvals is None:
            msg = approval_manager.RUNTIME_NOT_READY_REASON
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
    worker_lease_provider: Callable[[str | None], _WorkerManagerLease | None]
    api_enabled: bool = True
    retention_seconds: float = _DEFAULT_SCRIPT_RETENTION_SECONDS
    pass_timeout_seconds: float = 30.0
    pass_concurrency: int = 4
    reconcile_interval_seconds: float = 30.0
    _api_ready: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _start_requested: bool = field(default=False, init=False, repr=False)
    _activation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _worker_refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _startup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _maintenance_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _current_worker_lease: _WorkerManagerLease | None = field(default=None, init=False, repr=False)
    _pending_worker_lease: _PendingWorkerLease | None = field(default=None, init=False, repr=False)
    _startup_cleanup_pending: bool = field(default=False, init=False, repr=False)
    _reload_launch_fence_started: bool = field(default=False, init=False, repr=False)

    @property
    def tool_manager(self) -> ScriptRunManager | None:
        """Return the manager script tools use while this runtime serves them, else None.

        Serving starts when activation with the API begins its startup reconciliation and ends at the start of
        `unbind_api` or `shutdown`.
        """
        serving = self.api_enabled and self._api_ready.is_set() and self._started and self._start_requested
        return self.manager if serving else None

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
        """Request activation; it runs once the API is ready too."""
        self._start_requested = True
        if self.api_enabled and not self._api_ready.is_set():
            return
        await self._activate()

    async def _activate(self) -> None:
        """Activate once both composition roots have reported ready, and again after the API rebinds."""
        async with self._activation_lock:
            if self._started:
                return
            if not self._start_requested or (self.api_enabled and not self._api_ready.is_set()):
                return
            self.broker.close_call_admission()
            # A startup cleanup still pending from an earlier activation holds its fence until it succeeds.
            if not self._startup_cleanup_pending:
                await self.manager.begin_startup_reconciliation()
            self._started = True
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
        self.broker.close_call_admission()
        self._api_ready.clear()
        self.manager.gateway_url = ""
        startup_task, self._startup_task = self._startup_task, None
        await cancel_task(startup_task)
        maintenance_task, self._maintenance_task = self._maintenance_task, None
        await cancel_task(maintenance_task)
        self._started = False

    async def _maintenance_loop(self) -> None:
        while self._started:
            await asyncio.sleep(self.reconcile_interval_seconds)
            if not self._started:
                return
            try:
                await self._run_complete_pass()
            except Exception:
                logger.exception("script_maintenance_cycle_failed")

    async def _refresh_worker_backend(
        self,
        *,
        required_backend_locator: str | None = None,
    ) -> WorkerBackend | None:
        async with self._worker_refresh_lock:
            current = self._current_worker_lease
            if current is not None and (
                required_backend_locator is None or current.manager.cleanup_locator == required_backend_locator
            ):
                self._publish_worker_lease(current)
                return current.manager
            if current is not None:
                self._publish_worker_lease(None)
                await asyncio.to_thread(current.release)
            lease = await self._acquire_current_worker_lease(required_backend_locator)
            if lease is None:
                self._publish_worker_lease(None)
                return None
            if required_backend_locator is not None and lease.manager.cleanup_locator != required_backend_locator:
                await asyncio.to_thread(lease.release)
                self._publish_worker_lease(None)
                return None
            self._publish_worker_lease(lease)
            return lease.manager

    async def _acquire_current_worker_lease(
        self,
        required_backend_locator: str | None,
    ) -> _WorkerManagerLease | None:
        pending = self._get_or_create_worker_lease_acquisition(required_backend_locator)
        try:
            lease = await asyncio.shield(pending.task)
        except Exception:
            if self._pending_worker_lease is pending:
                self._pending_worker_lease = None
            raise
        if self._pending_worker_lease is pending:
            self._pending_worker_lease = None
        if lease is not None and not pending.delivery.acknowledge(lease):
            return None
        return lease

    def _get_or_create_worker_lease_acquisition(
        self,
        required_backend_locator: str | None,
    ) -> _PendingWorkerLease:
        pending = self._pending_worker_lease
        if pending is not None:
            if pending.backend_locator != required_backend_locator:
                msg = "Concurrent worker lease acquisitions require the same durable cleanup locator."
                raise RuntimeError(msg)
            return pending

        delivery = _WorkerLeaseDelivery()
        task = asyncio.create_task(
            asyncio.to_thread(delivery.acquire, self.worker_lease_provider, required_backend_locator),
            name="script_worker_backend_acquire",
        )
        task.add_done_callback(delivery.settle_task)
        pending = _PendingWorkerLease(task, delivery, required_backend_locator)
        self._pending_worker_lease = pending
        return pending

    async def reopen_script_calls(self) -> None:
        """Let running scripts call tools again once a reload's runtime update has run.

        New launches stay fenced until ``finish_reload``: they launch from the applied config, which the reload
        publishes only after its whole update returns.
        """
        async with self._worker_refresh_lock:
            if self._reload_launch_fence_started:
                self._open_call_admission_when_ready()

    async def finish_reload(self) -> None:
        """Release the reload launch fence once the reload's config is published as applied, or the reload failed."""
        # Cancellation while waiting for the lock must not leave launches fenced.
        await run_coroutine_until_complete(self._end_reload_launch_fence())

    async def _end_reload_launch_fence(self) -> None:
        """Finish the accepted fence release before reopening broker admission."""
        async with self._worker_refresh_lock:
            if not self._reload_launch_fence_started:
                return
            await self.manager.end_startup_reconciliation()
            self._reload_launch_fence_started = False
            self._open_call_admission_when_ready()

    def _open_call_admission_when_ready(self) -> None:
        if self.api_enabled and self._api_ready.is_set() and not self._startup_cleanup_pending:
            self.broker.open_call_admission()

    def _publish_worker_lease(self, lease: _WorkerManagerLease | None) -> None:
        """Publish the held lease and the manager's backend together without yielding."""
        self._current_worker_lease = lease
        self.manager.worker_backend = None if lease is None else lease.manager

    async def _release_current_worker_lease(self) -> None:
        lease = self._current_worker_lease
        self._publish_worker_lease(None)
        if lease is not None:
            await asyncio.to_thread(lease.release)

    async def apply_update_plan(self, plan: ConfigUpdatePlan) -> None:
        """Revoke removed owners and process-isolation changes before bot replacement."""
        await self._apply_update(
            plan.new_config,
            removed_entities=plan.remove,
            authorization_changed=plan.responder_access_changed,
            plugins_changed=bool(plan.plugin_changes),
        )

    async def interrupt_for_plugin_reload(self) -> None:
        """Interrupt every active run before a live plugin reload replaces the code they call."""
        current_config = self.config_provider()
        if current_config is not None:
            await self._apply_update(
                current_config,
                removed_entities=frozenset(),
                authorization_changed=False,
                plugins_changed=True,
            )

    async def _apply_update(
        self,
        new_config: Config,
        *,
        removed_entities: frozenset[str],
        authorization_changed: bool,
        plugins_changed: bool,
    ) -> None:
        current_config = self.config_provider()
        if current_config is None:
            return
        # The worker manager's identity is its backend settings, which a config reload never changes, and script
        # workers use per-run keys no other call ensures, so a reload leaves their processes running.
        removed_agents = set(removed_entities) & set(current_config.agents)
        continuing_agents = set(current_config.agents) & set(new_config.agents)
        isolation_changes = {
            agent_name
            for agent_name in continuing_agents
            if current_config.agents[agent_name].private != new_config.agents[agent_name].private
        }
        script_tool_removals = {
            agent_name
            for agent_name in continuing_agents
            if _agent_has_script_tool(current_config, agent_name) and not _agent_has_script_tool(new_config, agent_name)
        }
        process_authority_changes = {
            agent_name
            for agent_name in continuing_agents
            if script_process_authority(current_config, agent_name) != script_process_authority(new_config, agent_name)
        }
        if (
            not removed_agents
            and not isolation_changes
            and not script_tool_removals
            and not process_authority_changes
            and not authorization_changed
            and not plugins_changed
        ):
            return

        launch_fence_started = False

        async def apply_update_boundary() -> None:
            nonlocal launch_fence_started
            async with self._worker_refresh_lock:
                if not self._reload_launch_fence_started:
                    await self.manager.begin_startup_reconciliation()
                    self._reload_launch_fence_started = True
                    launch_fence_started = True
                self.broker.close_call_admission()
            await self._apply_update_pass(
                removed_agents=removed_agents,
                isolation_changes=isolation_changes,
                script_tool_removals=script_tool_removals,
                process_authority_changes=process_authority_changes,
                authorization_config=new_config if authorization_changed else None,
                plugins_changed=plugins_changed,
            )

        try:
            await asyncio.wait_for(
                apply_update_boundary(),
                timeout=self.pass_timeout_seconds,
            )
        except BaseException as exc:
            if launch_fence_started:
                await self.finish_reload()
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
        process_authority_changes: set[str],
        authorization_config: Config | None,
        plugins_changed: bool,
    ) -> None:
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        unauthorized_ids = {
            run.run_id
            for run in runs
            if authorization_config is not None
            and self.resolver.is_authorized(run, config=authorization_config) is not True
        }
        affected_agents = removed_agents | isolation_changes | script_tool_removals | process_authority_changes
        affected = (
            runs
            if plugins_changed
            else [run for run in runs if run.run_id in unauthorized_ids or run.agent_name in affected_agents]
        )
        await self._interrupt_runs(
            affected,
            reason_for=lambda run: (
                OWNER_AUTHORIZATION_REVOKED
                if run.run_id in unauthorized_ids
                else _reload_reason_for(
                    run,
                    removed_agents=removed_agents,
                    isolation_changes=isolation_changes,
                    script_tool_removals=script_tool_removals,
                    process_authority_changes=process_authority_changes,
                    plugins_changed=plugins_changed,
                )
            ),
        )

        unfinished = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        if plugins_changed and unfinished:
            msg = "Plugin reload did not interrupt every active background script."
            raise _ScriptRuntimeLifecycleError(msg)
        # A worker run the reload could not stop keeps what its worker can reach and the reload takes away, such as
        # knowledge mounts, so the reload fails instead of publishing over it. A revoked owner or script tool is
        # enforced through the broker, which already refuses the run's calls.
        reach_changed_agents = removed_agents | isolation_changes | process_authority_changes
        reach_changed_run_ids = {
            run.run_id for run in affected if not run.local_unsafe and run.agent_name in reach_changed_agents
        }
        if any(run.run_id in reach_changed_run_ids for run in unfinished):
            msg = "Config reload did not stop every background script whose worker access it changed."
            raise _ScriptRuntimeLifecycleError(msg)

    async def _interrupt_runs(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        reason_for: Callable[[ScriptRunRecord], str],
    ) -> None:
        """Durably revoke, close broker work, and reconcile selected processes in phases."""
        await run_coroutine_until_complete(self._durably_revoke_runs(runs, reason_for=reason_for))
        await self._interrupt_durably_revoked_runs(runs, reason_for=reason_for)

    async def _durably_revoke_runs(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        reason_for: Callable[[ScriptRunRecord], str],
    ) -> None:
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
            msg = "Background script cleanup did not durably revoke every active run."
            raise _ScriptRuntimeLifecycleError(msg)

    async def _interrupt_durably_revoked_runs(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        reason_for: Callable[[ScriptRunRecord], str],
    ) -> None:
        """Run independent broker/process obligations before exact resource finalization."""
        semaphore = asyncio.Semaphore(self.pass_concurrency)

        async def interrupt_run(run: ScriptRunRecord) -> None:
            async with semaphore:
                broker_result, process_result = await asyncio.gather(
                    self.manager.revoke(
                        run.run_id,
                        reason=reason_for(run),
                    ),
                    self.manager.reconcile_revoked_process(run_id=run.run_id),
                    return_exceptions=True,
                )
                process_reconciled = not isinstance(process_result, BaseException)
                if not process_reconciled:
                    logger.warning(
                        "script_reload_process_reconciliation_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=process_result,
                    )
                final_broker_result = broker_result
                if process_reconciled and isinstance(broker_result, BaseException):
                    (final_broker_result,) = await asyncio.gather(
                        self.manager.revoke(run.run_id, reason=reason_for(run)),
                        return_exceptions=True,
                    )
                broker_revoked = not isinstance(final_broker_result, BaseException)
                if not broker_revoked:
                    logger.warning(
                        "script_reload_broker_revocation_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=final_broker_result,
                    )
                    if final_broker_result is not broker_result:
                        logger.warning(
                            "script_reload_initial_broker_revocation_failed",
                            run_id=run.run_id,
                            agent_name=run.agent_name,
                            exc_info=broker_result,
                        )
                if not broker_revoked or not process_reconciled:
                    return
                (finalization_result,) = await asyncio.gather(
                    self.manager.reconcile_durable(run_id=run.run_id, broker_revoked=True),
                    return_exceptions=True,
                )
                if isinstance(finalization_result, BaseException):
                    logger.warning(
                        "script_reload_resource_finalization_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=finalization_result,
                    )

        await asyncio.gather(*(interrupt_run(run) for run in runs))

    async def _reconcile_pass(self) -> None:
        try:
            await self._refresh_worker_backend()
        except WorkerBackendError:
            logger.warning("script_worker_backend_refresh_pending", exc_info=True)
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        current_config = self.config_provider()
        unauthorized = [
            run
            for run in runs
            if (current_config is not None and run.agent_name not in current_config.agents)
            or self.resolver.is_authorized(run, config=current_config) is False
        ]
        if unauthorized:
            await self._interrupt_runs(
                unauthorized,
                reason_for=lambda _run: OWNER_AUTHORIZATION_REVOKED,
            )
            unauthorized_ids = {run.run_id for run in unauthorized}
            runs = [run for run in runs if run.run_id not in unauthorized_ids]
        backend = self.manager.worker_backend
        if backend is not None:
            worker_keys = dict.fromkeys(
                run.worker_key
                for run in runs
                if run.worker_key is not None and self.manager.worker_backend_for(run) is backend
            )
            await asyncio.gather(*(self._touch_worker(backend, worker_key) for worker_key in worker_keys))
        semaphore = asyncio.Semaphore(self.pass_concurrency)

        async def reconcile_run(run: ScriptRunRecord) -> None:
            async with semaphore:
                try:
                    await self.manager.reconcile_durable(run_id=run.run_id)
                except ScriptRunManagerError:
                    logger.warning(
                        "script_run_reconciliation_pending",
                        run_id=run.run_id,
                        agent_name=run.agent_name,
                        exc_info=True,
                    )

        await asyncio.gather(*(reconcile_run(run) for run in runs))

    async def _startup_cleanup_pass(self) -> None:
        """Adopt compatible surviving workers and retire other inherited ownership."""
        inherited = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        recoverable = [run for run in inherited if self._preserves_process(run)]
        recoverable_ids = {run.run_id for run in recoverable}
        retired = [run for run in inherited if run.run_id not in recoverable_ids]
        await self._durably_revoke_runs(retired, reason_for=lambda _run: RUNTIME_RESTARTED)
        await self._interrupt_runs_through_owning_backends(retired, reason_for=lambda _run: RUNTIME_RESTARTED)
        adopted_ids = await self._recover_startup_runs(recoverable)
        unfinished = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        pending = [run for run in unfinished if run.run_id not in adopted_ids]
        if pending:
            logger.error(
                "script_startup_cleanup_blocked",
                run_ids=[run.run_id for run in pending],
            )
            return
        if not adopted_ids:
            await self._release_current_worker_lease()
        if self.api_enabled:
            try:
                await self._refresh_worker_backend()
            except WorkerBackendError:
                logger.warning("script_worker_backend_refresh_pending", exc_info=True)
        await self.manager.end_startup_reconciliation()
        self._startup_cleanup_pending = False
        self._open_call_admission_when_ready()

    async def active_runs(self) -> list[ActiveScriptRunInfo]:
        """Read unfinished runs and whether this runtime's restart startup would adopt each process.

        A run is recoverable only when startup's adoption checks pass now without side effects:
        its recovery signature matches the current backend, configuration, and gateway, and its
        owner authorization is confirmed. Anything unresolved stays interruptible.
        """
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        config = self.config_provider()
        return [
            ActiveScriptRunInfo(
                run_id=run.run_id,
                responder=run.agent_name,
                requester_id=run.owner_user_id,
                recoverable=(
                    config is not None
                    and self._preserves_process(run)
                    and run_matches_recovery_signature(
                        run=run,
                        backend=self.manager.worker_backend_for(run),
                        config=config,
                        gateway_url=self.manager.gateway_url,
                    )
                    and self.resolver.is_authorized(run, config=config) is True
                ),
            )
            for run in runs
        ]

    def _preserves_process(self, run: ScriptRunRecord) -> bool:
        """Retain new Kubernetes process ownership while its primary is detached."""
        return (
            self.api_enabled
            and script_gateway_isolated(self.runtime_paths)
            and not run.local_unsafe
            and run.recovery_signature is not None
            and run.cancel_requested_at is None
            and run.finished_at is None
            and script_execution_uses_worker(self.runtime_paths, worker_backend_configured=True)
        )

    async def _recover_startup_runs(self, runs: Sequence[ScriptRunRecord]) -> set[str]:
        """Verify inherited process authority without creating or updating a worker template."""
        adopted: set[str] = set()
        config = self.config_provider()
        if config is None:
            return adopted
        for run in runs:
            try:
                backend = await self._refresh_worker_backend(required_backend_locator=run.worker_backend_locator)
                if backend is not None and run.worker_key is not None:
                    await asyncio.to_thread(backend.touch_worker, run.worker_key)
                if not run_matches_recovery_signature(
                    run=run,
                    backend=backend,
                    config=config,
                    gateway_url=self.manager.gateway_url,
                ):
                    await self.manager.revoke(run.run_id, reason=WORKER_CONFIGURATION_CHANGED)
                    await self.manager.reconcile_durable(run_id=run.run_id)
                    continue
                authorized = self.resolver.is_authorized(run, config=config)
                if authorized is None:
                    continue
                if not authorized:
                    await self.manager.revoke(run.run_id, reason=OWNER_AUTHORIZATION_REVOKED)
                reconciled = await self.manager.reconcile_durable(run_id=run.run_id)
                if reconciled.state is ScriptRunState.RUNNING and reconciled.cancel_requested_at is None:
                    # A previous primary may have died after claiming a tool call.
                    # Settle that ownership without replaying it or revoking the surviving process.
                    await self.broker.cancel_run(run.run_id)
                    adopted.add(run.run_id)
            except (ScriptRunManagerError, ScriptRunStoreError, WorkerBackendError, _ScriptRuntimeUnavailableError):
                logger.warning("script_startup_recovery_pending", run_id=run.run_id, exc_info=True)
        return adopted

    async def _interrupt_runs_through_owning_backends(
        self,
        runs: Sequence[ScriptRunRecord],
        *,
        reason_for: Callable[[ScriptRunRecord], str],
    ) -> None:
        """Reconcile each worker-backed run only through its durable cleanup locator."""
        await self._interrupt_durably_revoked_runs(
            [run for run in runs if run.local_unsafe],
            reason_for=reason_for,
        )
        grouped: dict[str | None, list[ScriptRunRecord]] = {}
        for run in runs:
            if not run.local_unsafe:
                grouped.setdefault(run.worker_backend_locator, []).append(run)
        for locator, backend_runs in grouped.items():
            try:
                await self._refresh_worker_backend(
                    required_backend_locator=locator,
                )
            except WorkerBackendError:
                self._publish_worker_lease(None)
                logger.warning("script_worker_backend_owner_refresh_pending", exc_info=True)
            await self._interrupt_durably_revoked_runs(
                backend_runs,
                reason_for=reason_for,
            )

    async def _touch_worker(self, backend: WorkerBackend, worker_key: str) -> None:
        try:
            await asyncio.to_thread(backend.touch_worker, worker_key)
        except WorkerBackendError:
            logger.warning("script_worker_touch_pending", worker=worker_log_id(worker_key), exc_info=True)

    def touch_live_workers(self, backend: WorkerBackend) -> None:
        """Refresh active-run worker leases immediately before idle cleanup."""
        for worker_key in sorted(
            {
                run.worker_key
                for run in self.store.list_runs(include_finished=False)
                if run.worker_key is not None and run.worker_backend_locator == backend.cleanup_locator
            },
        ):
            try:
                backend.touch_worker(worker_key)
            except WorkerBackendError:
                logger.warning(
                    "script_worker_touch_pending",
                    worker=worker_log_id(worker_key),
                    exc_info=True,
                )
                raise

    async def _prune_pass(self, *, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=self.retention_seconds)
        finished_before = utc_timestamp(cutoff)
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=True)
        for run in runs:
            if run.state not in TERMINAL_SCRIPT_RUN_STATES or run.finished_at is None:
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

    async def _run_complete_pass(self) -> None:
        try:
            await asyncio.wait_for(self._complete_pass(), timeout=self.pass_timeout_seconds)
        except TimeoutError:
            logger.warning("script_maintenance_pass_timeout", timeout_seconds=self.pass_timeout_seconds)

    async def shutdown(self, *, timeout_seconds: float = _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Stop serving script tools, then run bounded final reconciliation."""
        self.broker.close_call_admission()
        shutdown_deadline = asyncio.get_running_loop().time() + timeout_seconds
        self._start_requested = False
        startup_task, self._startup_task = self._startup_task, None
        await cancel_task(startup_task)
        maintenance_task, self._maintenance_task = self._maintenance_task, None
        await cancel_task(maintenance_task)

        try:
            await run_coroutine_until_complete(self.manager.begin_shutdown())
            runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
            preserved = [run for run in runs if self._preserves_process(run)]
            preserved_ids = {run.run_id for run in preserved}
            revoked = [run for run in runs if run.run_id not in preserved_ids]
            await run_coroutine_until_complete(
                self._durably_revoke_runs(revoked, reason_for=lambda _run: RUNTIME_SHUTDOWN),
            )
            await self._run_shutdown_cleanup(
                revoked,
                preserved_runs=preserved,
                deadline=shutdown_deadline,
                timeout_seconds=timeout_seconds,
            )
        finally:
            self._started = False
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
        preserved_runs: Sequence[ScriptRunRecord],
    ) -> None:
        try:
            await asyncio.wait_for(
                self._interrupt_and_prune_for_shutdown(runs),
                timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except TimeoutError:
            logger.warning("script_shutdown_reconciliation_timeout", timeout_seconds=timeout_seconds)
        if preserved_runs:
            await self.broker.detach_runs(
                {run.run_id for run in preserved_runs},
                timeout_seconds=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        cleanup_drained = await drain_script_tool_cleanup(
            self.broker,
            timeout_seconds=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
        if not cleanup_drained:
            logger.warning("script_shutdown_tool_cleanup_timeout", timeout_seconds=timeout_seconds)

    async def _interrupt_and_prune_for_shutdown(self, runs: Sequence[ScriptRunRecord]) -> None:
        await self._interrupt_runs_through_owning_backends(
            runs,
            reason_for=lambda _run: RUNTIME_SHUTDOWN,
        )
        await self._prune_pass()

    def _detach_worker_leases(self) -> list[_WorkerManagerLease]:
        leases = [self._current_worker_lease] if self._current_worker_lease is not None else []
        pending = self._pending_worker_lease
        if pending is not None:
            pending_lease = pending.delivery.abandon()
            if pending_lease is not None:
                leases.append(pending_lease)
            if pending.task.done() and not pending.task.cancelled():
                pending_failure = pending.task.exception()
                if pending_failure is not None:
                    logger.warning("script_worker_backend_pending_acquire_failed", exc_info=pending_failure)
        self._pending_worker_lease = None
        self._publish_worker_lease(None)
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
    worker_lease_provider: Callable[[str | None], _WorkerManagerLease | None],
    api_enabled: bool,
) -> ScriptRuntimeLifecycle:
    """Construct the one process-local script store, resolver, broker, and manager."""
    store = ScriptRunStore(runtime_paths)
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=runtime_paths,
        bot_provider=bot_provider,
        # The manager, built below, publishes the one current backend.
        worker_backend_provider=lambda run: manager.worker_backend_for(run),
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=ScriptWorkerClient(),
        worker_backend=None,
        gateway_url="",
    )
    return ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=config_provider,
        worker_lease_provider=worker_lease_provider,
        api_enabled=api_enabled,
        retention_seconds=_script_retention_seconds(runtime_paths),
    )


def _run_execution_identity(run: ScriptRunRecord) -> ToolExecutionIdentity:
    """Parse the durable Matrix execution identity a run was launched with."""
    return parse_tool_execution_identity_payload(
        run.execution_identity,
        error_prefix="Background script execution_identity",
    )


def _agent_has_script_tool(config: Config, agent_name: str) -> bool:
    """Return whether one agent's effective tool surface includes script controls."""
    return "script" in config.resolve_agent(agent_name).tools.tool_names


def _reload_reason_for(
    run: ScriptRunRecord,
    *,
    removed_agents: set[str],
    isolation_changes: set[str],
    script_tool_removals: set[str],
    process_authority_changes: set[str],
    plugins_changed: bool,
) -> str:
    if plugins_changed:
        return PLUGIN_TOOLS_CHANGED
    if run.agent_name in removed_agents:
        return OWNER_AGENT_REMOVED
    if run.agent_name in isolation_changes:
        return AGENT_ISOLATION_CHANGED
    if run.agent_name in script_tool_removals:
        return SCRIPT_TOOL_REMOVED
    if run.agent_name in process_authority_changes:
        return WORKER_CONFIGURATION_CHANGED
    return OWNER_AUTHORIZATION_REVOKED


def _script_retention_seconds(runtime_paths: RuntimePaths) -> float:
    raw = runtime_paths.env_value(_SCRIPT_RETENTION_SECONDS_ENV, default=str(_DEFAULT_SCRIPT_RETENTION_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        msg = f"{_SCRIPT_RETENTION_SECONDS_ENV} must be a positive number"
        raise ValueError(msg)
    return value


async def _script_gateway_url(runtime_paths: RuntimePaths, *, host: str, port: int) -> str:
    """Return the gateway URL injected into isolated script processes."""
    worker_process_enabled = script_execution_uses_worker(
        runtime_paths,
        worker_backend_configured=worker_backend_kind(runtime_paths).dedicated,
    )
    if not worker_process_enabled:
        gateway_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104
        if ":" in gateway_host:
            gateway_host = f"[{gateway_host}]"
        return f"http://{gateway_host}:{port}/api/script-gateway"
    explicit_url = (runtime_paths.env_value("MINDROOM_SCRIPT_GATEWAY_URL") or "").strip()
    if explicit_url:
        gateway_url = explicit_url.rstrip("/")
        await _validate_script_gateway(gateway_url)
        return gateway_url
    if public_url := configured_public_url(runtime_paths):
        gateway_url = f"{public_url}/api/script-gateway"
        await _validate_script_gateway(gateway_url)
        return gateway_url
    msg = "Background-script workers require MINDROOM_SCRIPT_GATEWAY_URL or MINDROOM_PUBLIC_URL."
    raise ValueError(msg)


async def optional_script_gateway_url(runtime_paths: RuntimePaths, *, host: str, port: int) -> str:
    """Resolve the optional gateway without failing the API for an unrelated public URL."""
    try:
        return await _script_gateway_url(runtime_paths, host=host, port=port)
    except ValueError:
        explicit_url = (runtime_paths.env_value("MINDROOM_SCRIPT_GATEWAY_URL") or "").strip()
        if explicit_url:
            raise
        if configured_public_url(runtime_paths):
            logger.warning("background_script_gateway_disabled_invalid_public_url")
        return ""


async def _validate_script_gateway(gateway_url: str) -> None:
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
