"""Managed tool-job lifecycle and durable Matrix completion delivery."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.constants import HOOK_SOURCE_KEY, ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.custom_tools.job import is_job_function
from mindroom.delegation.background import delegation_child, reconcile_delegation
from mindroom.delegation.lifecycle import active_delegation_edges
from mindroom.delegation.recovery import interrupt_child
from mindroom.dispatch_source import HOOK_DISPATCH_SOURCE_KIND
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.tool_job_completion import TOOL_JOB_COMPLETION_KEY
from mindroom.tool_jobs.authorization import function_authority, locally_allowed
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
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.tools.function import Function

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)
_RETRY_SECONDS = 5.0


def _build_completion_content(
    job: BackgroundJob,
    recipient_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, Any]:
    """Build one exact-recipient event for normal durable Matrix ingress."""
    body = f"Background tool job `{job.job_id}` ({job.tool_name}) is {job.status}."
    body += f' Call job(action="wait", job_id="{job.job_id}") to retrieve its stored result or approval request.'
    if job.owner.transport_agent_name and job.owner.transport_agent_name != job.owner.agent_name:
        body += f" Ask member {job.owner.agent_name} to make this call; the job belongs to that member."
    content = format_message_with_mentions(
        config,
        runtime_paths,
        body,
        thread_event_id=job.owner.resolved_thread_id,
        latest_thread_event_id=job.owner.resolved_thread_id,
        extra_content={
            ORIGINAL_SENDER_KEY: job.owner.requester_id,
            SOURCE_KIND_KEY: HOOK_DISPATCH_SOURCE_KIND,
            HOOK_SOURCE_KEY: "tool_job_completion",
            TOOL_JOB_COMPLETION_KEY: {
                "job_id": job.job_id,
                "generation": job.generation,
                "transaction_id": f"tool_job_{job.job_id}_{job.generation}",
            },
        },
    )
    # Child text may mention other agents; only the exact parent receives this turn.
    content["m.mentions"] = {"user_ids": [recipient_user_id]}
    return content


@dataclass
class ToolJobRuntimeCoordinator:
    """Own background jobs and deliver outcomes through ordinary serialized ingress."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], AgentBot | TeamBot | None]
    agent_reply_memberships: AgentReplyMembershipIndex
    _runtime: ToolJobRuntime | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

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
            child_name = delegation_child(job).child_agent_name
            if child_name not in caller.delegate_to:
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
        if recipient != owner.agent_name:
            team = config.teams.get(recipient)
            if team is None or owner.agent_name not in team.agents:
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
        """Recheck retained functions after cooperative human holds release."""
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
        team = config.teams.get(recipient)
        valid_transport = recipient == root or (team is not None and root in team.agents)
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
        if self.config_provider() is None:
            await self.stop()
            return
        if self._task is None or self._task.done():
            if self._task is not None and not self._task.cancelled():
                error = self._task.exception()
                if error is not None:
                    logger.error("Tool job delivery worker stopped; restarting", error=str(error))
            await self.runtime.recover()
            register_background_runtime(self.runtime_paths, self.runtime)
            set_execution_authorizer(self._authorize_execution)
            self._task = asyncio.create_task(self._run(), name="tool_job_completion_worker")
        self.runtime.changed.set()

    async def stop(self) -> None:
        """Withdraw admission and stop delivery before cancelling owned child work."""
        register_background_runtime(self.runtime_paths, None)
        set_execution_authorizer(None)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._runtime is not None:
            await self._runtime.shutdown()
            self._runtime = None

    async def _run(self) -> None:
        while True:
            self.runtime.changed.clear()
            await self.deliver_pending()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.runtime.changed.wait(), timeout=_RETRY_SECONDS)

    async def deliver_pending(self) -> None:
        """Retry pending notices, retaining their durable claim on ambiguous sends."""
        for job in await self.runtime.pending_deliveries():
            try:
                await self._deliver(job)
            except Exception:
                logger.exception("Background tool job completion delivery failed", job_id=job.job_id)

    async def _deliver(self, job: BackgroundJob) -> None:
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
        joined_rooms = await get_joined_rooms(client)
        if joined_rooms is None or job.owner.room_id not in joined_rooms:
            return
        config = self.config_provider()
        if config is None or not self._authorized(job):
            return
        recipient_user_id = bot.matrix_id.full_id
        delivery = await self.runtime.claim_delivery(
            job.job_id,
            content=_build_completion_content(job, recipient_user_id, config, self.runtime_paths),
            transaction_id=f"tool_job_{job.job_id}_{job.generation}",
        )
        if delivery is None or delivery.acknowledged:
            return
        # A replaced Matrix account cannot reuse another account's transaction scope.
        if delivery.content.get("m.mentions") != {"user_ids": [recipient_user_id]}:
            await self.runtime.block_delivery(job.job_id, delivery.transaction_id)
            return
        if self.bot_provider(recipient) is not bot or not bot.running or not self._authorized(job):
            return
        delivered = await send_message_result(
            client,
            job.owner.room_id,
            delivery.content,
            transaction_id=delivery.transaction_id,
        )
        if delivered is not None:
            await self.runtime.acknowledge_delivery(job.job_id, delivery.transaction_id, event_id=delivered.event_id)
