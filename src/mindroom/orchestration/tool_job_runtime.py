"""Managed tool-job lifecycle: recovery, revocation, saved Stops, card denial, held-message wakes, and retention."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.custom_tools.job import is_job_function
from mindroom.delegation.background import delegation_child, reconcile_delegation
from mindroom.delegation.job_approvals import prune_child_approvals, settle_child_approvals
from mindroom.delegation.lifecycle import active_delegation_edges
from mindroom.delegation.recovery import interrupt_stopped_child
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.authorization import function_authority, locally_allowed
from mindroom.tool_jobs.disabled import index_parked_work
from mindroom.tool_jobs.held_replies import conversation_work, decode_held_reply, waiting_notice
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
    """Own the background job runtime and wake the held messages whose work changed; replies deliver outcomes."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], AgentBot | TeamBot | None]
    agent_reply_memberships: AgentReplyMembershipIndex
    # The event journal jobs are kept in; it is read only once a config exists, and outlives this coordinator.
    journal_provider: Callable[[], EventJournalStore]
    _instance: ToolJobInstance | None = field(default=None, init=False)
    _runtime: ToolJobRuntime | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _initialized: bool = field(default=False, init=False)
    _journal: EventJournalStore | None = field(default=None, init=False)
    # Recovered jobs a Stop saved while the runtime was away could still change, by recipient, until that recipient's
    # bot exists to read its journal.
    _unrestored_stops: dict[str, list[BackgroundJob]] = field(default_factory=dict, init=False)

    async def initialize(self) -> None:
        """Pin execution mode, then build the job runtime or index parked ownership, before dispatch can start."""
        if self._initialized:
            return
        config = self.config_provider()
        if config is None:
            return
        self._journal = journal = self.journal_provider()
        self._instance = instance = pin_background_tool_jobs(config, self.runtime_paths)
        if instance.settings.enabled:
            if self._runtime is None:
                store = journal.tool_jobs(uuid4().hex)
                # Take the jobs over before dispatch starts, fencing any runtime that still writes from before;
                # the first sync recovers them, and a later one never takes them back from a newer process.
                await store.take_ownership()
                self._runtime = ToolJobRuntime(
                    store,
                    authorize=self._authorized,
                    authorize_execution=self._authorize_execution,
                    cancel=self._interrupt_child,
                    denied=self._denied,
                )
        else:
            instance.parked = await index_parked_work(journal)
        self._initialized = True

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
        outcome = await reconcile_delegation(
            job,
            cleanup=partial(
                interrupt_stopped_child,
                config=config,
                runtime_paths=self.runtime_paths,
                cancel_reason="Background execution was cancelled; tools were not replayed.",
            ),
            runtime_paths=self.runtime_paths,
        )
        # A job interrupted while it waited for approvals leaves its cards answerable until they are denied.
        await settle_child_approvals(self.runtime, job.job_id)
        return outcome

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
        await self.initialize()
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
                    logger.error("Tool job worker stopped; restarting", error=str(error))
            await runtime.recover()
            self._unrestored_stops = {}
            for job in await runtime.stoppable_jobs():
                self._unrestored_stops.setdefault(job.owner.recipient, []).append(job)
            await self._restore_user_stops()
            register_background_runtime(self.runtime_paths, runtime)
            self._task = asyncio.create_task(self._run(), name="tool_job_worker")
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
        """Stop the worker and execution while live response owners finish their receipts."""
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
            self._unrestored_stops.clear()

    async def _run(self) -> None:
        next_retention = 0.0
        while True:
            self.runtime.changed.clear()
            try:
                await self._reconcile()
                if asyncio.get_running_loop().time() >= next_retention:
                    await self._expire_consumed_results()
                    next_retention = asyncio.get_running_loop().time() + 3600
            except Exception:
                logger.exception("Background tool job reconciliation failed; retrying")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.runtime.changed.wait(), timeout=_RETRY_SECONDS)

    async def _reconcile(self) -> None:
        """Stop revoked work, apply saved Stops, deny interrupted jobs' cards, and wake held messages; retry failures."""
        await self.runtime.cancel_revoked(denied=self._denied)
        await self._restore_user_stops()
        for job_id in tuple(self.runtime.unsettled_approvals):
            await settle_child_approvals(self.runtime, job_id)
        await self._wake_held_replies()

    async def _wake_held_replies(self) -> None:
        """Admit one wake per saved hold whose work became ready, ended, or now waits for something else."""
        journal = self._journal
        if journal is None:
            return
        holds = journal.held_replies()
        for saved in await holds.load_all():
            if saved.woken_generation == saved.generation:
                continue
            try:
                hold = decode_held_reply(saved)
            except ValueError:
                logger.exception(
                    "Unreadable held reply; it waits for a newer reply to replace it",
                    hold_id=saved.hold_id,
                )
                continue
            if hold.key.recipient in self._unrestored_stops:
                # A Stop saved while the runtime was away may still end this work.
                continue
            work = await conversation_work(self.runtime, hold.key, attempted=hold.offered)
            if not hold.stopped and work.jobs and not work.ready and waiting_notice(work.jobs) == hold.notice:
                continue
            bot = self.bot_provider(hold.key.recipient)
            # Synced membership: a bot outside the room costs no homeserver request on every pass.
            if bot is None or not bot.running or bot.client is None or hold.key.room_id not in bot.client.rooms:
                continue
            try:
                await bot.wake_held_reply(hold)
                await holds.mark_woken(hold.key.hold_id, hold.generation)
            except Exception:
                logger.exception("Waking a held reply failed; retrying", hold_id=saved.hold_id)

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
            sources = {source for source in (job.source_event_id, job.consumed_by_source) if source is not None}
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

        expired = await self.runtime.expire_consumed(
            before=datetime.now(UTC) - CONSUMED_RESULT_RETENTION,
            source_finished=source_finished,
        )
        await prune_child_approvals(expired)
