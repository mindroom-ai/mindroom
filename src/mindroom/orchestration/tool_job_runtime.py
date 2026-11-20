"""Managed tool-job lifecycle: recovery, revocation, follow-ups, and retention."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.constants import CONVERSATION_CONTENT_KEY
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import delegation_child, reconcile_delegation
from mindroom.delegation.continuation import continued_subagent_job
from mindroom.delegation.lifecycle import active_delegation_edges, caller_grants_child, delegation_grant
from mindroom.delegation.recovery import interrupt_stopped_child
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import turn_rows
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.message_target import MessageTarget
from mindroom.tool_jobs.approvals import end_tool_call_approval, prune_job_approvals
from mindroom.tool_jobs.authorization import locally_allowed, toolkit_offered
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import follow_up_jobs, held_jobs, held_notice, next_follow_up_id
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.publication import JobStatePublisher
from mindroom.tool_jobs.runtime import (
    TERMINAL_STATUSES,
    BackgroundJob,
    BackgroundOutcome,
    JobAccessError,
    JobGrant,
    JobSpec,
    ToolJobRuntime,
    register_background_runtime,
)
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore
    from mindroom.tool_jobs.execution_authority import JobTool
    from mindroom.tool_jobs.instances import ToolJobInstance
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
# How long the worker sleeps between passes without a change, which also sets how soon a running job past its grace is listed.
_RETRY_SECONDS = 5.0
# How long a stopped subagent job waits for its requester's access to resolve once its bot has synced.
_ACCESS_WAIT_SECONDS = 600.0


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


def _is_subagent_job(job: BackgroundJob) -> bool:
    """Return whether a restart leaves the job running to continue it: a subagent's turn continues, a tool call never."""
    return job.kind == "delegation"


def _follow_up_candidates(jobs: Iterable[BackgroundJob]) -> dict[ConversationKey, tuple[BackgroundJob, ...]]:
    """Group the ended top-level jobs a follow-up or held-jobs notice may report by conversation, before access checks.

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
    return {key: tuple(groups[key]) for key in sorted(groups, key=_key_order)}


async def _post_held_notice(client: nio.AsyncClient, key: ConversationKey, txn: str, text: str) -> bool:
    """Post the agent's notice that the limit holds jobs; a failed send is retried on a later pass, as the same transaction."""
    # An m.notice starts no turn; as a conversation a room-level one is listed, and a room-mode reply keeps its key.
    content = build_message_content(
        text,
        thread_event_id=key.thread_id,
        latest_thread_event_id=key.thread_id,
        extra_content={"msgtype": "m.notice", CONVERSATION_CONTENT_KEY: True},
    )
    delivered = await send_message_result(client, key.room_id, content, operation="send_job_hold", transaction_id=txn)
    return delivered is not None


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
    config_provider: Callable[[], Config]
    bot_provider: Callable[[str], AgentBot | None]
    agent_reply_memberships: AgentReplyMembershipIndex
    # The event journal jobs are kept in; it outlives this coordinator.
    journal_provider: Callable[[], EventJournalStore]
    # Whether an entity's bot failed to start for good, so a job that waits for it to continue ends instead.
    entity_unavailable: Callable[[str], bool] = lambda _entity_name: False
    # When each stopped job's requester's access was first found unresolved under a synced bot, by the loop's clock.
    _unresolved_since: dict[str, float] = field(default_factory=dict, init=False)
    _instance: ToolJobInstance | None = field(default=None, init=False)
    _runtime: ToolJobRuntime | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _initialized: bool = field(default=False, init=False)
    _journal: EventJournalStore | None = field(default=None, init=False)
    _publisher: JobStatePublisher = field(default_factory=JobStatePublisher, init=False)
    # Follow-ups this process admitted for jobs still waiting to be reported, which later passes leave alone.
    _admitted_follow_ups: set[str] = field(default_factory=set, init=False)
    # Held-jobs notices this process posted, by the jobs each names; kept until one of those jobs is reported or gone.
    _sent_notices: dict[str, frozenset[str]] = field(default_factory=dict, init=False)

    async def initialize(self) -> None:
        """Pin execution mode, then build the job runtime when the instance runs jobs, before dispatch can start."""
        if self._initialized:
            return
        config = self.config_provider()
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
                grant=self._grant,
                authorize_execution=self._authorize_execution,
                cancel=self._interrupt,
                continues=_is_subagent_job,
                reply_stopped=self._reply_stopped,
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
        """Settle a stopped job's own cleanup and reconcile a subagent's child; the save of its end denies its cards."""
        if job.kind != "delegation":
            # A gated tool call that runs as a job may wait on its approval card.
            return end_tool_call_approval(job)
        config = self.config_provider()
        return await reconcile_delegation(
            job,
            cleanup=partial(
                interrupt_stopped_child,
                config=config,
                runtime_paths=self.runtime_paths,
                cancel_reason="Background execution was cancelled; tools were not replayed.",
            ),
            runtime_paths=self.runtime_paths,
        )

    async def _reply_stopped(self, job: BackgroundJob) -> bool:
        """Return whether the reply that started a job recorded a Stop since the job began.

        The reply's record is the authority for a Stop: it holds one the process that recorded it did not live to
        apply to the job. A reply stopped and regenerated before the job began does not count.
        """
        if job.reply_id is None or (bot := self.bot_provider(job.owner.recipient)) is None:
            return False
        reply = await bot.journal_principal().replies.load(job.reply_id)
        return reply is not None and reply.stop_receipt_order != job.reply_stop_order

    def _grant(self, job: BackgroundJob) -> JobGrant:
        """Recheck current delegation, team membership, and requester reply access."""
        config = self.config_provider()
        owner = job.owner
        if owner.channel != "matrix" or owner.requester_id is None or owner.room_id is None:
            return "denied"
        if owner.agent_name not in config.agents:
            return "denied"
        entities = _accessed_entities(config, owner.recipient, owner.agent_name)
        tools = config_plugins(config, self.runtime_paths).tools
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
        elif not (
            # A run's job answers for the run, not for one call: its agent must still have the toolkit.
            toolkit_offered(config, owner, tools=tools, name="workflow", depth=job.depth)
            if job.kind == "workflow"
            else locally_allowed(
                config,
                owner,
                tools=tools,
                tool_name=job.tool_name,
                toolkit_name=job.toolkit_name,
                origin=job.adapter.get("origin", {}),
                depth=job.depth,
                authority=job.adapter.get("authority", {}),
            )
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
    ) -> JobGrant:
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
        tool: JobTool,
        arguments: Mapping[str, Any] | None = None,
    ) -> None:
        """Recheck retained functions immediately before application execution."""
        if isinstance(tool.toolkit, JobTools):
            return  # Every action checks its exact stored owner through the runtime.

        origin = function_provenance(tool, arguments)
        config = self.config_provider()
        edges = active_delegation_edges(owner)
        if not locally_allowed(
            config,
            owner,
            tools=config_plugins(config, self.runtime_paths).tools,
            tool_name=tool.function.name,
            toolkit_name=tool.toolkit_name,
            origin=origin,
            depth=len(edges),
            authority=dict(tool.authority),
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
            # The jobs it interrupted leave the rooms' state now, not at the next start, which may have jobs off; so
            # do the subagent jobs it left running in storage for the next start to continue.
            ended = [job for job in self._runtime.retained_jobs() if job.status in TERMINAL_STATUSES]
            await self._publisher.publish(self.bot_provider, ended)

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
        """Stop revoked work, continue what a restart stopped, publish live jobs, and admit follow-ups."""
        await self.runtime.cancel_revoked()
        runtime = self.runtime
        await self._continue_stopped_jobs(runtime)
        await self._publisher.publish(self.bot_provider, runtime.retained_jobs(), awaited=runtime.awaited_job_ids())
        await self._admit_follow_ups(self.runtime)

    async def _continue_stopped_jobs(self, runtime: ToolJobRuntime) -> None:
        """Continue each subagent job a restart stopped, or end it interrupted when it cannot go on.

        One job that fails here is retried on the next pass and does not hold up the others.
        """
        jobs = runtime.awaiting_continuation()
        waiting = {job.job_id for job in jobs}
        self._unresolved_since = {job_id: at for job_id, at in self._unresolved_since.items() if job_id in waiting}
        for job in jobs:
            try:
                await self._continue_stopped_job(runtime, job)
            except Exception:
                logger.exception("Subagent job a restart stopped was not settled; retrying", job_id=job.job_id)

    async def _continue_stopped_job(self, runtime: ToolJobRuntime, job: BackgroundJob) -> None:
        """Continue one stopped job, end it, or leave it for a later pass while its bot or its access is not ready."""
        bot = self._ready_bot(job)
        if bot is None:
            return
        spec: JobSpec | str
        if isinstance(bot, str):
            spec = bot
        else:
            context = self._conversation_context(bot, job)
            if context is None:
                return
            # The job's task starts in this context, so its child runs in its conversation as a reply's job does.
            with tool_runtime_context(context):
                try:
                    spec = await continued_subagent_job(job, runtime)
                except Exception:
                    logger.exception("Subagent job could not continue after a restart", job_id=job.job_id)
                    spec = "its continuation could not be prepared"
                if not isinstance(spec, str):
                    await runtime.continue_stopped(job.job_id, spec)
                    return
        logger.info("Subagent job ends instead of continuing after a restart", job_id=job.job_id, reason=spec)
        await runtime.continue_stopped(job.job_id, None)

    def _ready_bot(self, job: BackgroundJob) -> AgentBot | str | None:
        """Return the bot a stopped job continues under, why the job ends instead, or None while the job waits.

        A bot is ready once it has completed its first sync: by then its client holds the rooms it is in, and
        room-backed reply grants can resolve, which the first pass runs too early for. A bot that is only late keeps
        its job waiting; one whose start failed for good, or that is no longer in the job's room, ends it. Access
        that stays unresolved under a synced bot, as a grant room its control client left does, ends the job after a
        bounded wait; access proven gone is revocation's to cancel.
        """
        recipient = job.owner.recipient
        if self.entity_unavailable(recipient):
            return f"the bot of {recipient!r} could not start"
        bot = self.bot_provider(recipient)
        if bot is None or not bot.running or not bot.first_sync_complete or bot.client is None:
            return None
        if job.owner.room_id not in bot.client.rooms:
            return f"the bot of {recipient!r} is no longer in the job's room"
        grant = self._grant(job)
        if grant == "allowed":
            return bot
        now = asyncio.get_running_loop().time()
        if grant == "pending" and now - self._unresolved_since.setdefault(job.job_id, now) >= _ACCESS_WAIT_SECONDS:
            return "its requester's access stayed unresolved after the bot's first sync"
        return None

    def _conversation_context(self, bot: AgentBot, job: BackgroundJob) -> ToolRuntimeContext | None:
        """Rebuild the tool context of the conversation a job belongs to, or None while its bot has no Matrix client."""
        owner = job.owner
        if owner.room_id is None or owner.session_id is None:
            return None
        target = MessageTarget(
            room_id=owner.room_id,
            source_thread_id=owner.thread_id,
            resolved_thread_id=owner.resolved_thread_id,
            reply_to_event_id=None,
            session_id=owner.session_id,
        )
        context = bot.tool_runtime_support.build_context(
            target,
            user_id=owner.requester_id,
            agent_name=owner.agent_name,
        )
        if context is None:
            return None
        # The job keeps the turn that started it, so a silent schedule's subagent stays silent.
        return replace(context, membership_turn_id=job.source_event_id, source_kind=job.source_kind)

    async def _admit_follow_ups(self, runtime: ToolJobRuntime) -> None:
        """Admit one follow-up per conversation whose ended jobs no reply collected, at most the limit in a row.

        A job a follow-up turn started counts that follow-up, so work that keeps starting work stops at the limit;
        a job a human message started counts none. Jobs only the limit holds get one notice instead.
        """
        due: set[str] = set()
        for key, group in _follow_up_candidates(runtime.retained_jobs()).items():
            # Jobs an admitted follow-up or a sent notice already reports need no access check again.
            candidates, held = follow_up_jobs(group), held_jobs(group)
            known = {next_follow_up_id(key, candidates)} if candidates else set()
            known |= {held_notice(key, held)[0]} if held else set()
            if not known or known <= self._admitted_follow_ups | self._sent_notices.keys():
                due |= known
                continue
            # The jobs the requester may still access decide between a follow-up and a notice of jobs the limit holds.
            ended = await runtime.ended_jobs(key)
            if (bot := self.bot_provider(key.recipient)) is None:
                continue
            if jobs := follow_up_jobs(ended):
                follow_up_id = next_follow_up_id(key, jobs)
                if follow_up_id not in self._admitted_follow_ups:
                    await bot.admit_job_follow_up(key, follow_up_id)
                    self._admitted_follow_ups.add(follow_up_id)
                due.add(follow_up_id)
            elif (held := held_jobs(ended)) and bot.client is not None:
                notice_id, text = held_notice(key, held)
                if notice_id in self._sent_notices or await _post_held_notice(bot.client, key, notice_id, text):
                    self._sent_notices[notice_id] = frozenset(job.job_id for job in held)
        # Forget follow-ups whose jobs were reported since, so a long-running process does not keep them all.
        self._admitted_follow_ups &= due
        waiting = {job.job_id for group in _follow_up_candidates(runtime.retained_jobs()).values() for job in group}
        self._sent_notices = {notice: ids for notice, ids in self._sent_notices.items() if ids <= waiting}

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
                if await journal.turn_records(entity).read(turn_rows.is_answered, source):
                    finished[key] = True
                # A turn that ended unanswered, as when its request was deleted, finished once its source settled.
                elif (bot := self.bot_provider(entity)) is not None:
                    principal = bot.journal_principal()
                    admitted = await principal.load_event(source)
                    finished[key] = admitted is not None and not await principal.is_pending(source)
                else:
                    finished[key] = False
            return finished[key]

        expired = await self.runtime.expire_ended(now=datetime.now(UTC), source_finished=source_finished)
        await prune_job_approvals(expired)
