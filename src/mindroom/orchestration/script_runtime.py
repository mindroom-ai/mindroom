"""Focused lifecycle ownership for the primary background-script runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from mindroom import approval_manager
from mindroom.custom_tools.script import bind_script_run_manager
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.script_runs.broker import ScriptRuntimeWorkerAuthority, ScriptToolBroker
from mindroom.script_runs.manager import ScriptRunManager, ScriptRunManagerError
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunStore
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


@dataclass(slots=True)
class _LiveScriptRuntimeResolver:
    """Rebuild current runtime, worker, and approval authority for a durable run."""

    runtime_paths: RuntimePaths
    bot_provider: Callable[[str], AgentBot | None]
    worker_backend_provider: Callable[[], WorkerBackend | None]
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
            backend = self.worker_backend_provider()
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


@dataclass(slots=True)
class ScriptRuntimeLifecycle:
    """Keep one broker/manager pair stable while runtime generations change."""

    runtime_paths: RuntimePaths
    store: ScriptRunStore
    broker: ScriptToolBroker
    manager: ScriptRunManager
    resolver: _LiveScriptRuntimeResolver
    config_provider: Callable[[], Config | None]
    worker_backend_provider: Callable[[], WorkerBackend | None]
    api_enabled: bool = True
    retention_seconds: float = _DEFAULT_SCRIPT_RETENTION_SECONDS
    _api_ready: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _start_requested: bool = field(default=False, init=False, repr=False)
    _activation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _startup_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

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
            self._refresh_worker_backend()
            bind_script_run_manager(self.manager)
            self._started = True
            await self.reconcile_once()
            await self.prune_once()

    def _refresh_worker_backend(self) -> WorkerBackend | None:
        backend = self.worker_backend_provider()
        self.manager.worker_backend = backend
        return backend

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
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        for run in runs:
            if run.agent_name not in removed_agents | isolation_changes:
                continue
            try:
                context = self.resolver.resolve(run, correlation_id=f"background-script:{run.run_id}:lifecycle")
                if run.agent_name in removed_agents:
                    await self.manager.cancel(context, run_id=run.run_id, reason=_REMOVED_AGENT_REASON)
                else:
                    await self.manager.interrupt(context, run_id=run.run_id)
            except (ScriptRunManagerError, ScriptWorkerError, _ScriptRuntimeUnavailableError):
                logger.warning(
                    "script_reload_reconciliation_pending",
                    run_id=run.run_id,
                    agent_name=run.agent_name,
                    exc_info=True,
                )

    async def reconcile_once(self) -> None:  # privata: ignore -- explicit lifecycle sweep API.
        """Touch live workers first, then reconcile every resolvable unfinished run."""
        backend = self._refresh_worker_backend()
        runs = await asyncio.to_thread(self.store.list_runs, include_finished=False)
        if backend is not None:
            for worker_key in sorted({run.worker_key for run in runs if run.worker_key is not None}):
                await asyncio.to_thread(backend.touch_worker, worker_key)
        for run in runs:
            try:
                context = self.resolver.resolve(run, correlation_id=f"background-script:{run.run_id}:reconcile")
                await self.manager.reconcile(context, run_id=run.run_id)
            except (ScriptRunManagerError, ScriptWorkerError, _ScriptRuntimeUnavailableError):
                logger.warning(
                    "script_run_reconciliation_pending",
                    run_id=run.run_id,
                    agent_name=run.agent_name,
                    exc_info=True,
                )

    def touch_live_workers(self, backend: WorkerBackend) -> None:
        """Refresh active-run worker leases immediately before idle cleanup."""
        for worker_key in sorted(
            {run.worker_key for run in self.store.list_runs(include_finished=False) if run.worker_key is not None},
        ):
            backend.touch_worker(worker_key)

    async def prune_once(  # privata: ignore -- deterministic retention sweep API.
        self,
        *,
        now: datetime | None = None,
    ) -> None:
        """Prune terminal snapshots, receipts, and run rows after retention."""
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
                context = self.resolver.resolve(run, correlation_id=f"background-script:{run.run_id}:retention")
                cleaned = await self.manager.cleanup_snapshot(context, run)
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

    async def shutdown(self, *, timeout_seconds: float = 5.0) -> None:
        """Run bounded final reconciliation, then clear the process-local tool binding."""
        self._start_requested = False
        startup_task, self._startup_task = self._startup_task, None
        await cancel_task(startup_task)
        if not self._started:
            return

        async def _cleanup() -> None:
            await self.reconcile_once()
            await self.prune_once()

        try:
            await asyncio.wait_for(_cleanup(), timeout=timeout_seconds)
        except TimeoutError:
            logger.warning("script_shutdown_reconciliation_timeout", timeout_seconds=timeout_seconds)
        finally:
            bind_script_run_manager(None)
            self._started = False


def build_script_runtime(
    runtime_paths: RuntimePaths,
    *,
    config_provider: Callable[[], Config | None],
    bot_provider: Callable[[str], AgentBot | None],
    worker_backend_provider: Callable[[], WorkerBackend | None],
    api_enabled: bool,
) -> ScriptRuntimeLifecycle:
    """Construct the one process-local script store, resolver, broker, and manager."""
    store = ScriptRunStore(runtime_paths)
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=runtime_paths,
        bot_provider=bot_provider,
        worker_backend_provider=worker_backend_provider,
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
    return ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=config_provider,
        worker_backend_provider=worker_backend_provider,
        api_enabled=api_enabled,
        retention_seconds=retention_seconds,
    )


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
    if value <= 0:
        msg = f"{_SCRIPT_RETENTION_SECONDS_ENV} must be a positive number"
        raise ValueError(msg)
    return value


def script_gateway_url(runtime_paths: RuntimePaths, *, host: str, port: int) -> str:
    """Return the gateway URL injected into isolated script processes."""
    explicit_url = (runtime_paths.env_value("MINDROOM_SCRIPT_GATEWAY_URL") or "").strip()
    if explicit_url:
        return explicit_url.rstrip("/")
    public_url = (runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").strip()
    if public_url:
        return f"{public_url.rstrip('/')}/api/script-gateway"
    gateway_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host  # noqa: S104
    return f"http://{gateway_host}:{port}/api/script-gateway"
