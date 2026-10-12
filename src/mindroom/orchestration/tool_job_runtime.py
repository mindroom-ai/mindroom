"""Managed tool-job lifecycle: recovery, revocation, card denial, and retention."""

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
from mindroom.delegation.lifecycle import active_delegation_edges, caller_grants_child, delegation_grant
from mindroom.delegation.recovery import interrupt_stopped_child
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.approvals import end_tool_call_approval, prune_job_approvals, settle_job_approvals
from mindroom.tool_jobs.authorization import function_authority, locally_allowed
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import follow_up_jobs, next_follow_up_id
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.publication import JobStatePublisher
from mindroom.tool_jobs.runtime import (
    ENDED_JOB_RETENTION,
    TERMINAL_STATUSES,
    BackgroundJob,
    BackgroundOutcome,
    JobAccessError,
    ToolJobRuntime,
    register_background_runtime,
)

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


def _follow_up_candidates(jobs: Iterable[BackgroundJob]) -> dict[ConversationKey, tuple[BackgroundJob, ...]]:
    """Group the ended top-level jobs a follow-up may report by conversation, before checking anyone's access.

    A silent schedule's work waits for its next silent run, which opens with the note instead.
    """
    groups: dict[ConversationKey, list[BackgroundJob]] = {}
    for job in jobs:
        if (
            job.status in TERMINAL_STATUSES
            and job.depth == 0
            and not job.consumed
            and job.owner.room_id is not None
            and job.owner.requester_id is not None
            and job.source_kind != SILENT_SCHEDULE_SOURCE_KIND
        ):
            key = ConversationKey(
                recipient=job.owner.recipient,
                room_id=job.owner.room_id,
                thread_id=job.owner.resolved_thread_id,
                requester_id=job.owner.requester_id,
                silent=False,
            )
            groups.setdefault(key, []).append(job)
    candidates = {key: follow_up_jobs(group) for key, group in groups.items()}
    return {key: candidates[key] for key in sorted(candidates, key=_key_order) if candidates[key]}


def _key_order(key: ConversationKey) -> tuple[str, str, str, str]:
    return (key.recipient, key.room_id, key.thread_id or "", key.requester_id)


def _accessed_entities(config: Config, recipient: str, actor: str) -> set[str]:
    """Return the entities the requester must still reach: a configured team's access grants its exact members."""
    team = config.teams.get(recipient)
    if team is not None and actor in team.agents:
        return {recipient}
    return {recipient, actor}


@dataclass
class ToolJobRuntimeCoordinator:
    """Own the background job runtime; replies deliver outcomes."""

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
    _publisher: JobStatePublisher = field(default_factory=JobStatePublisher, init=False)
    # Follow-ups this process admitted for jobs still waiting to be reported, which later passes leave alone.
    _admitted_follow_ups: set[str] = field(default_factory=set, init=False)

    async def initialize(self) -> None:
        """Pin execution mode, then build the job runtime when the instance runs jobs, before dispatch can start."""
        if self._initialized:
            return
        config = self.config_provider()
        if config is None:
            return
        self._journal = journal = self.journal_provider()
        self._instance = instance = pin_background_tool_jobs(config, self.runtime_paths)
        # With background jobs off, saved jobs wait untouched for the next enabled start, which reports them.
        if instance.settings.enabled:
            store = journal.tool_jobs(uuid4().hex)
            # Take the jobs over before dispatch starts, fencing any runtime that still writes from before;
            # the first sync recovers them, and a later one never takes them back from a newer process.
            await store.take_ownership()
            self._runtime = ToolJobRuntime(
                store,
                authorize=self._authorized,
                authorize_execution=self._authorize_execution,
                cancel=self._interrupt,
                denied=self._denied,
            )
        self._initialized = True

    @property
    def runtime(self) -> ToolJobRuntime:
        """The job owner an enabled instance created at startup; a stopped or disabled coordinator has none."""
        if self._runtime is None:
            msg = "Background tool jobs are not running."
            raise RuntimeError(msg)
        return self._runtime

    async def _interrupt(self, job: BackgroundJob) -> BackgroundOutcome | None:
        """Settle a stopped job's own cleanup: deny the cards it waits on, and reconcile a subagent's child."""
        if job.kind != "delegation":
            # A gated tool call that runs as a job may wait on its approval card.
            return await end_tool_call_approval(self.runtime, job)
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
        await settle_job_approvals(self.runtime, job.job_id)
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
        entities = _accessed_entities(config, owner.recipient, owner.agent_name)
        if job.kind == "delegation":
            child = delegation_child(job)
            child_name = child.child_agent_name
            if (
                not caller_grants_child(owner.agent_name, child_name, config, delegation_grant(child))
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
        # Delegated children and their callers need their own grants; the root actor may hold its team's.
        callers = ({owner.agent_name, *(caller for caller, _, _ in edges)} - {root}) | _accessed_entities(
            config,
            recipient,
            root,
        )
        # A workflow participant is granted by its caller's workflow tool, any other child by its delegate list.
        allowed_edges = all(caller_grants_child(caller, child, config, grant) for caller, child, grant in edges)
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
            register_background_runtime(self.runtime_paths, runtime)
            self._task = asyncio.create_task(self._run(), name="tool_job_worker")
        runtime.changed.set()

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

    async def _run(self) -> None:
        next_retention = 0.0
        while True:
            self.runtime.changed.clear()
            try:
                await self._reconcile()
                if asyncio.get_running_loop().time() >= next_retention:
                    await self._expire_ended_jobs()
                    next_retention = asyncio.get_running_loop().time() + 3600
            except Exception:
                logger.exception("Background tool job reconciliation failed; retrying")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.runtime.changed.wait(), timeout=_RETRY_SECONDS)

    async def _reconcile(self) -> None:
        """Stop revoked work, publish live jobs, deny interrupted jobs' cards, and admit follow-ups."""
        await self.runtime.cancel_revoked(denied=self._denied)
        await self._publisher.publish(self.bot_provider, self.runtime.retained_jobs())
        for job_id in tuple(self.runtime.unsettled_approvals):
            await settle_job_approvals(self.runtime, job_id)
        await self._admit_follow_ups(self.runtime)

    async def _admit_follow_ups(self, runtime: ToolJobRuntime) -> None:
        """Admit one follow-up per conversation whose ended jobs no reply collected, at most the limit in a row.

        A job a follow-up turn started counts that follow-up, so work that keeps starting work stops at the limit;
        a job a human message started counts none.
        """
        due: set[str] = set()
        for key, candidates in _follow_up_candidates(runtime.retained_jobs()).items():
            # Jobs an admitted follow-up already reports need no access check again.
            follow_up_id = next_follow_up_id(key, candidates)
            if follow_up_id not in self._admitted_follow_ups:
                jobs = follow_up_jobs(await runtime.ended_jobs(key))
                bot = self.bot_provider(key.recipient)
                if not jobs or bot is None:
                    continue
                follow_up_id = next_follow_up_id(key, jobs)
                if follow_up_id not in self._admitted_follow_ups:
                    await bot.admit_job_follow_up(key, follow_up_id)
                    self._admitted_follow_ups.add(follow_up_id)
            due.add(follow_up_id)
        # Forget follow-ups whose jobs were reported since, so a long-running process does not keep them all.
        self._admitted_follow_ups &= due

    async def _expire_ended_jobs(self) -> None:
        """Keep ended jobs for the retention period and as long as response or approval work owns them."""
        journal = self._journal
        if journal is None:
            return
        protected_sessions: set[tuple[str, str]] = set()
        cursor: str | None = None
        while owners := await journal.approval_continuations(limit=100, after=cursor):
            protected_sessions.update((owner.entity_name, owner.session_id) for _principal, owner in owners)
            cursor = owners[-1][1].approval_id
        finished: dict[tuple[str, str], bool] = {}

        async def source_finished(job: BackgroundJob) -> bool:
            entity, source = job.owner.recipient, job.source_event_id
            if source is None or (entity, job.owner.session_id) in protected_sessions:
                return False
            # The turn that started the job may still need its outcome to recover.
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
            return finished[key]

        expired = await self.runtime.expire_ended(
            before=datetime.now(UTC) - ENDED_JOB_RETENTION,
            source_finished=source_finished,
        )
        await prune_job_approvals(expired)
