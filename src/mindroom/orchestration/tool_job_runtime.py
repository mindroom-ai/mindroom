"""Managed tool-job lifecycle and quiet serialized conversation wakeups."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from mindroom import approval_manager
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.custom_tools.job import is_job_function
from mindroom.delegation.background import delegation_child, reconcile_delegation
from mindroom.delegation.lifecycle import active_delegation_edges
from mindroom.delegation.recovery import interrupt_child
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.authorization import function_authority, locally_allowed
from mindroom.tool_jobs.disabled import index_parked_work
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.runtime import (
    CONSUMED_RESULT_RETENTION,
    BackgroundJob,
    BackgroundOutcome,
    JobAccessError,
    ToolJobRuntime,
    register_background_runtime,
)
from mindroom.tool_jobs.user_stop import restore_user_stops

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from agno.tools.function import Function

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore
    from mindroom.tool_jobs.instances import ToolJobInstance
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
_RETRY_SECONDS = 5.0


def _transport_allows_actor(config: Config, recipient: str, actor: str) -> bool:
    """Validate a runtime-owned actor against its configured or ad hoc transport."""
    if recipient == actor:
        return True
    team = config.teams.get(recipient)
    if team is not None:
        return actor in team.agents
    # Ad hoc teams use an ordinary agent's Matrix account. Their actual members
    # are materialized by the team driver; requester access is rechecked below.
    return recipient in config.agents and actor in config.agents


@dataclass
class ToolJobRuntimeCoordinator:
    """Own background jobs and wake their serialized conversation response owner."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], AgentBot | TeamBot | None]
    agent_reply_memberships: AgentReplyMembershipIndex
    _instance: ToolJobInstance | None = field(default=None, init=False)
    _runtime: ToolJobRuntime | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _initialized: bool = field(default=False, init=False)
    # Journal admission of a completion is idempotent, but each one is a write transaction that locks its room's
    # membership row and wakes the dispatcher, so every retry pass would repeat it until the job is consumed.
    _admitted: set[tuple[str, int]] = field(default_factory=set, init=False)
    _journal: EventJournalStore | None = field(default=None, init=False)
    # Recovered jobs a Stop saved while the runtime was away could still change, by recipient, until that recipient's
    # bot exists to read its journal; no completion is delivered to a recipient still listed here.
    _unrestored_stops: dict[str, list[BackgroundJob]] = field(default_factory=dict, init=False)
    # Jobs whose approval cards still need expiring; a failed expiry retries next pass.
    _withdrawn_approvals: set[str] = field(default_factory=set, init=False)

    async def initialize(self, journal: EventJournalStore | None = None) -> None:
        """Pin execution mode, then claim job storage or index parked ownership, before dispatch can start."""
        if self._initialized:
            return
        self._journal = journal
        config = self.config_provider()
        if config is None:
            return
        self._instance = instance = pin_background_tool_jobs(config, self.runtime_paths)
        if instance.settings.enabled:
            # The thread itself keeps the runtime, so a cancelled startup leaves its lease for a retry to reuse
            # and for stop to release.
            if self._runtime is None:
                await run_blocking_until_complete(self._claim_storage)
        else:
            instance.parked = await index_parked_work(self.runtime_paths, journal)
        self._initialized = True

    def _claim_storage(self) -> None:
        self._runtime = ToolJobRuntime(
            self.runtime_paths.storage_root,
            authorize=self._authorized,
            authorize_execution=self._authorize_execution,
            cancel=self._interrupt_child,
        )

    @property
    def runtime(self) -> ToolJobRuntime:
        """The job owner an enabled instance created at startup; a stopped or disabled coordinator has none."""
        if self._runtime is None:
            msg = "Background tool jobs are not running."
            raise RuntimeError(msg)
        return self._runtime

    async def _interrupt_child(self, job: BackgroundJob) -> BackgroundOutcome | None:
        if job.kind != "delegation":
            return None
        config = self.config_provider()
        if config is None:
            msg = "Cannot settle a background job without runtime configuration."
            raise RuntimeError(msg)
        return await reconcile_delegation(
            job,
            cleanup=partial(
                interrupt_child,
                config=config,
                runtime_paths=self.runtime_paths,
                reason="Background execution was cancelled or interrupted; tools were not replayed.",
            ),
            runtime_paths=self.runtime_paths,
        )

    def _authorized(self, job: BackgroundJob) -> bool:
        """Grant access only on a proven grant; unresolved membership fails closed."""
        return self._grant(job) == "allowed"

    def _denied(self, job: BackgroundJob) -> bool:
        """Revoke execution only on a proven denial, never while membership is still resolving."""
        return self._grant(job) == "denied"

    def _grant(self, job: BackgroundJob) -> Literal["allowed", "denied", "pending"]:
        """Recheck current delegation, team membership, and requester reply access."""
        config = self.config_provider()
        owner = job.owner
        if config is None or owner.channel != "matrix" or owner.requester_id is None or owner.room_id is None:
            return "denied"
        caller = config.agents.get(owner.agent_name)
        if caller is None:
            return "denied"
        entities = {owner.agent_name, owner.recipient}
        if job.kind == "delegation":
            child = delegation_child(job)
            child_name = child.child_agent_name
            if (
                child_name not in caller.delegate_to
                or child_name not in config.agents
                or child.storage_bindings != freeze_delegation_storage(config, child.storage_bindings)
            ):
                return "denied"
            entities.add(child_name)
        elif not locally_allowed(
            config,
            owner,
            tool_name=job.tool_name,
            toolkit_name=job.toolkit_name,
            origin=job.adapter.get("origin", {}),
            depth=job.depth,
            authority=job.adapter.get("authority", {}),
        ):
            return "denied"
        if not _transport_allows_actor(config, owner.recipient, owner.agent_name):
            return "denied"
        return self._requester_access(config, owner.requester_id, owner.room_id, entities)

    def _requester_access(
        self,
        config: Config,
        requester_id: str,
        room_id: str | None,
        entities: Iterable[str],
    ) -> Literal["allowed", "denied", "pending"]:
        """Whether the requester may still converse with every entity, or membership is still resolving."""
        pending = False
        for entity_name in entities:
            try:
                if not is_sender_allowed_for_responder(
                    requester_id,
                    entity_name,
                    room_id,
                    config,
                    self.runtime_paths,
                    self.agent_reply_memberships,
                    require_resolved_membership=True,
                ):
                    return "denied"
            except ReplyMembershipPendingError:
                pending = True
        return "pending" if pending else "allowed"

    def _authorize_execution(
        self,
        owner: ToolExecutionIdentity,
        function: Function,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        """Recheck retained functions immediately before application execution."""
        if is_job_function(function):
            return  # Every action checks its exact stored owner through the runtime.

        origin = function_provenance(function, arguments)
        config = self.config_provider()
        edges = active_delegation_edges(owner)
        if config is None or not locally_allowed(
            config,
            owner,
            tool_name=function.name,
            toolkit_name=function.owning_toolkit,
            origin=origin,
            depth=len(edges),
            authority=function_authority(function),
        ):
            msg = "Tool execution is no longer authorized for this caller."
            raise JobAccessError(msg)
        root = edges[0][0] if edges else owner.agent_name
        recipient = owner.transport_agent_name or root
        valid_transport = _transport_allows_actor(config, recipient, root)
        callers = {owner.agent_name, recipient, *(caller for caller, _ in edges)}
        allowed_edges = all(
            caller in config.agents and child in config.agents[caller].delegate_to for caller, child in edges
        )
        # Accepted work keeps executing while membership resolves; only a proven denial stops it, as revocation does.
        if (
            not valid_transport
            or not allowed_edges
            or owner.requester_id is None
            or self._requester_access(config, owner.requester_id, owner.room_id, callers) == "denied"
        ):
            msg = "Tool execution is no longer authorized for this caller."
            raise JobAccessError(msg)

    async def sync(self) -> None:
        """Recover once, publish the service, and wake it after config changes."""
        await self.initialize(self._journal)
        config = self.config_provider()
        if config is None:
            await self.stop()
            return
        runtime = self._runtime
        if runtime is None:
            return
        if self._task is None or self._task.done():
            if self._task is not None and not self._task.cancelled():
                error = self._task.exception()
                if error is not None:
                    logger.error("Tool job completion worker stopped; restarting", error=str(error))
            await runtime.recover()
            self._unrestored_stops = {}
            if self._journal is not None:
                for job in await runtime.stoppable_jobs():
                    self._unrestored_stops.setdefault(job.owner.recipient, []).append(job)
            await self._restore_user_stops()
            register_background_runtime(self.runtime_paths, runtime)
            self._task = asyncio.create_task(self._run(), name="tool_job_completion_worker")
        else:
            await self._restore_user_stops()
        runtime.changed.set()

    async def _restore_user_stops(self) -> None:
        """Apply saved Stops to recovered jobs, for each recipient once its bot exists; a failed one retries next pass."""
        journal = self._journal
        for recipient, jobs in tuple(self._unrestored_stops.items()):
            bot = self.bot_provider(recipient)
            if journal is None or bot is None:
                continue
            try:
                await restore_user_stops(self.runtime, bot.journal_principal(), journal.turn_records(recipient), jobs)
            except Exception:
                logger.exception("Saved user Stop restoration failed; retrying", recipient=recipient)
                continue
            self._unrestored_stops.pop(recipient, None)

    async def quiesce(self) -> None:
        """Stop wakeups and execution while live response owners finish their receipts."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._runtime is not None:
            await self._runtime.quiesce()

    async def stop(self) -> None:
        """Release the service after response finalization has stopped using its receipts."""
        try:
            try:
                await self.quiesce()
            finally:
                if self._runtime is not None:
                    await self._runtime.shutdown()
        finally:
            if self._instance is not None:
                release_background_tool_jobs(self.runtime_paths, self._instance)
            self._instance = self._runtime = self._journal = None
            self._initialized = False
            self._admitted.clear()
            self._unrestored_stops.clear()
            self._withdrawn_approvals.clear()

    async def _run(self) -> None:
        next_retention = 0.0
        while True:
            self.runtime.changed.clear()
            try:
                await self.deliver_pending()
                if asyncio.get_running_loop().time() >= next_retention:
                    await self._expire_consumed_results()
                    next_retention = asyncio.get_running_loop().time() + 3600
            except Exception:
                logger.exception("Background tool job completion scan failed; retrying")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.runtime.changed.wait(), timeout=_RETRY_SECONDS)

    async def deliver_pending(self) -> None:
        """Retry pending outcomes until the durable journal owns each generation."""
        await self.runtime.cancel_revoked(denied=self._denied)
        await self._restore_user_stops()
        await self._expire_withdrawn_approval_cards()
        pending = await self.runtime.pending_outcomes()
        self._admitted.intersection_update((job.job_id, job.generation) for job in pending)
        for job in pending:
            try:
                await self._deliver(job)
            except Exception:
                logger.exception("Background tool job completion wakeup failed", job_id=job.job_id)

    async def _expire_withdrawn_approval_cards(self) -> None:
        """Expire the cards of job approval pauses that can never resume, so the replies waiting on them resume."""
        self._withdrawn_approvals |= self.runtime.take_withdrawn_approvals()
        manager = approval_manager.get_approval_store()
        if manager is None or (self._withdrawn_approvals and await manager.expire_job_cards(self._withdrawn_approvals)):
            self._withdrawn_approvals.clear()

    async def _expire_consumed_results(self) -> None:
        """Keep consumed jobs for the retention period and as long as response or approval work owns them."""
        journal = self._journal
        if journal is None:
            return
        protected_sessions: set[tuple[str, str]] = set()
        cursor: tuple[str, str] | None = None
        while owners := await journal.approval_continuations(limit=100, after=cursor):
            protected_sessions.update((owner.entity_name, owner.session_id) for _principal, owner in owners)
            cursor = (owners[-1][1].entity_name, owners[-1][1].approval_id)
        finished: dict[tuple[str, str], bool] = {}

        async def source_finished(job: BackgroundJob) -> bool:
            entity = job.owner.recipient
            if job.source_event_id is None or (entity, job.owner.session_id) in protected_sessions:
                return False
            # The reply that consumed the outcome may still need it to recover, like the turn that started the job.
            sources = {source for source in (job.source_event_id, job.consuming_source) if source is not None}
            for source in sources:
                key = (entity, source)
                if key not in finished:
                    record = await journal.turn_records(entity).load(source)
                    if record is not None:
                        finished[key] = record.completed
                    elif (bot := self.bot_provider(entity)) is not None:
                        principal = bot.journal_principal()
                        admitted = await principal.load_event(source)
                        finished[key] = admitted is not None and not await principal.is_pending(source)
                    else:
                        finished[key] = False
            return all(finished[(entity, source)] for source in sources)

        await self.runtime.expire_consumed(
            before=datetime.now(UTC) - CONSUMED_RESULT_RETENTION,
            source_finished=source_finished,
        )

    async def _deliver(self, job: BackgroundJob) -> None:
        generation = (job.job_id, job.generation)
        if generation in self._admitted or job.owner.recipient in self._unrestored_stops:
            return
        bot = self.bot_provider(job.owner.recipient)
        if (
            bot is None
            or not bot.running
            or bot.client is None
            or job.owner.room_id is None
            # Synced membership: a bot outside the room costs no homeserver request on every retry pass.
            or job.owner.room_id not in bot.client.rooms
            or not self._authorized(job)
        ):
            return
        current = await self.runtime.outcome(job.job_id, job.generation)
        if current is not None:
            await bot.wake_tool_job_completion(current)
            self._admitted.add(generation)
