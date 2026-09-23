"""Managed tool-job lifecycle and quiet serialized conversation wakeups."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.custom_tools.job import is_job_function
from mindroom.delegation.background import delegation_child, reconcile_delegation
from mindroom.delegation.lifecycle import active_delegation_edges
from mindroom.delegation.recovery import interrupt_child
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.tool_jobs.authorization import function_authority, locally_allowed
from mindroom.tool_jobs.disabled import clear_parked_work, index_parked_work
from mindroom.tool_jobs.execution_authority import set_execution_authorizer
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.runtime import (
    BackgroundJob,
    BackgroundOutcome,
    JobAccessError,
    ToolJobRuntime,
    get_background_runtime,
    register_background_runtime,
)
from mindroom.tool_jobs.settings import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.user_stop import restore_user_stops
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.tools.function import Function
    from nio import AsyncClient

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore
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
    _runtime: ToolJobRuntime | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _initialized: bool = field(default=False, init=False)
    _admitted: set[tuple[str, int]] = field(default_factory=set, init=False)
    _journal: EventJournalStore | None = field(default=None, init=False)

    async def initialize(self, journal: EventJournalStore | None = None) -> None:
        """Pin execution mode and index parked ownership before dispatch can start."""
        if self._initialized:
            return
        self._journal = journal
        config = self.config_provider()
        if config is None:
            return
        if not pin_background_tool_jobs(config, self.runtime_paths):
            await index_parked_work(self.runtime_paths, journal)
        self._initialized = True

    @property
    def runtime(self) -> ToolJobRuntime:
        """Claim storage only when the service starts using the job owner."""
        if self._runtime is None:
            self._runtime = ToolJobRuntime(
                self.runtime_paths.storage_root,
                authorize=self._authorized,
                cancel=self._interrupt_child,
            )
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
        """Recheck current delegation, team membership, and requester reply access."""
        config = self.config_provider()
        owner = job.owner
        if config is None or owner.channel != "matrix" or owner.requester_id is None or owner.room_id is None:
            return False
        caller = config.agents.get(owner.agent_name)
        if caller is None:
            return False
        entities = {owner.agent_name, owner.transport_agent_name or owner.agent_name}
        if job.kind == "delegation":
            child = delegation_child(job)
            child_name = child.child_agent_name
            if (
                child_name not in caller.delegate_to
                or child_name not in config.agents
                or child.storage_bindings != freeze_delegation_storage(config, child.storage_bindings)
            ):
                return False
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
            return False
        recipient = owner.transport_agent_name or owner.agent_name
        if not _transport_allows_actor(config, recipient, owner.agent_name):
            return False
        return all(
            is_sender_allowed_for_responder(
                owner.requester_id,
                entity_name,
                owner.room_id,
                config,
                self.runtime_paths,
                self.agent_reply_memberships,
            )
            for entity_name in entities
        )

    def _authorize_execution(
        self,
        owner: ToolExecutionIdentity,
        function: Function,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        """Recheck retained functions immediately before application execution."""
        context = get_tool_runtime_context()
        if context is None or get_background_runtime(context.runtime_paths) is not self._runtime:
            return
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
        if (
            not valid_transport
            or not allowed_edges
            or owner.requester_id is None
            or not all(
                is_sender_allowed_for_responder(
                    owner.requester_id,
                    name,
                    owner.room_id,
                    config,
                    self.runtime_paths,
                    self.agent_reply_memberships,
                )
                for name in callers
            )
        ):
            msg = "Tool execution is no longer authorized for this caller."
            raise JobAccessError(msg)

    async def sync(self) -> None:
        """Recover once, publish the service, and wake it after config changes."""
        await self.initialize()
        config = self.config_provider()
        if config is None:
            await self.stop()
            return
        if not pin_background_tool_jobs(config, self.runtime_paths):
            return
        if self._task is None or self._task.done():
            if self._task is not None and not self._task.cancelled():
                error = self._task.exception()
                if error is not None:
                    logger.error("Tool job completion worker stopped; restarting", error=str(error))
            await self.runtime.recover()
            if self._journal is not None:
                for entity_name in (*config.agents, *config.teams):
                    bot = self.bot_provider(entity_name)
                    if bot is not None:
                        await restore_user_stops(
                            self.runtime,
                            self._journal.principal(bot._journal_principal_id),
                            self._journal.turn_records(entity_name),
                        )
            register_background_runtime(self.runtime_paths, self.runtime)
            set_execution_authorizer(self.runtime_paths, self._authorize_execution)
            self._task = asyncio.create_task(self._run(), name="tool_job_completion_worker")
        self.runtime.changed.set()

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
            register_background_runtime(self.runtime_paths, None)
            set_execution_authorizer(self.runtime_paths, None)
            self._runtime = None
            release_background_tool_jobs(self.runtime_paths)
            clear_parked_work(self.runtime_paths)
            self._initialized = False
            self._journal = None
            self._admitted.clear()

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
        await self.runtime.cancel_revoked()
        memberships: dict[AsyncClient, list[str] | None] = {}
        pending = await self.runtime.pending_outcomes()
        self._admitted.intersection_update((job.job_id, job.generation) for job in pending)
        for job in pending:
            try:
                await self._deliver(job, memberships)
            except Exception:
                logger.exception("Background tool job completion wakeup failed", job_id=job.job_id)

    async def _expire_consumed_results(self) -> None:
        """Retain results for thirty days and as long as response or approval work owns them."""
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
            entity = job.owner.transport_agent_name or job.owner.agent_name
            source = job.adapter.get("source_event_id")
            if (entity, job.owner.session_id) in protected_sessions:
                return False
            if not isinstance(source, str):
                return job.legacy_source_untracked
            key = (entity, source)
            if key not in finished:
                record = await journal.turn_records(entity).load(source)
                if record is not None:
                    finished[key] = record.completed
                elif (bot := self.bot_provider(entity)) is not None:
                    principal = journal.principal(bot._journal_principal_id)
                    finished[key] = await principal.load_event(source) is not None and not await principal.is_pending(
                        source,
                    )
                else:
                    finished[key] = False
            return finished[key]

        await self.runtime.expire_consumed(
            before=datetime.now(UTC) - timedelta(days=30),
            source_finished=source_finished,
        )

    async def _deliver(self, job: BackgroundJob, memberships: dict[AsyncClient, list[str] | None]) -> None:
        generation = (job.job_id, job.generation)
        if generation in self._admitted:
            return
        recipient = job.owner.transport_agent_name or job.owner.agent_name
        bot = self.bot_provider(recipient)
        if (
            bot is None
            or not bot.running
            or bot.client is None
            or job.owner.room_id is None
            or not self._authorized(job)
        ):
            return
        client = bot.client
        if client not in memberships:
            memberships[client] = await get_joined_rooms(client)
        joined_rooms = memberships[client]
        if joined_rooms is None or job.owner.room_id not in joined_rooms:
            return
        if self.bot_provider(recipient) is not bot or not bot.running or not self._authorized(job):
            return
        current = await self.runtime.outcome(job.job_id, job.generation)
        if current is not None:
            await bot.wake_tool_job_completion(current)
            self._admitted.add(generation)
