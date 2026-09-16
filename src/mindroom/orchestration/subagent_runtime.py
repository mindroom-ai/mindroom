"""Managed delegation lifecycle and durable Matrix completion delivery."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.constants import HOOK_SOURCE_KEY, ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.delegation.background import BackgroundSubagentRuntime, register_background_runtime
from mindroom.delegation.recovery import interrupt_child
from mindroom.dispatch_source import HOOK_DISPATCH_SOURCE_KIND
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.mentions import format_message_with_mentions

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.background import BackgroundJob
    from mindroom.delegation.state import DelegationChild

logger = get_logger(__name__)
_RETRY_SECONDS = 5.0
_RESULT_PREVIEW_LENGTH = 8000


def _build_completion_content(
    job: BackgroundJob,
    recipient_user_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> dict[str, Any]:
    """Build one exact-recipient event for normal durable Matrix ingress."""
    body = f"Background subagent job `{job.job_id}` is {job.status}."
    if job.status == "awaiting_approval":
        body += f' Call wait_subagent(job_id="{job.job_id}") to review its approval request.'
    else:
        if job.result:
            body += "\n\n" + job.result[:_RESULT_PREVIEW_LENGTH]
        body += f'\n\nUse inspect_subagent(job_id="{job.job_id}") for the stored outcome.'
    content = format_message_with_mentions(
        config,
        runtime_paths,
        body,
        thread_event_id=job.owner.resolved_thread_id,
        latest_thread_event_id=job.owner.resolved_thread_id,
        extra_content={
            ORIGINAL_SENDER_KEY: job.owner.requester_id,
            SOURCE_KIND_KEY: HOOK_DISPATCH_SOURCE_KIND,
            HOOK_SOURCE_KEY: "subagent_completion",
        },
    )
    # Child text may mention other agents; only the exact parent receives this turn.
    content["m.mentions"] = {"user_ids": [recipient_user_id]}
    return content


@dataclass
class SubagentRuntimeCoordinator:
    """Own background jobs and deliver outcomes through ordinary serialized ingress."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], AgentBot | TeamBot | None]
    agent_reply_memberships: AgentReplyMembershipIndex
    _runtime: BackgroundSubagentRuntime | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    @property
    def runtime(self) -> BackgroundSubagentRuntime:
        """Claim storage only when the service starts using the job owner."""
        if self._runtime is None:
            self._runtime = BackgroundSubagentRuntime(
                self.runtime_paths.storage_root,
                authorize=self._authorized,
                cancel=self._interrupt_child,
            )
        return self._runtime

    async def _interrupt_child(self, child: DelegationChild) -> None:
        config = self.config_provider()
        if config is None:
            msg = "Cannot settle a background subagent without runtime configuration."
            raise RuntimeError(msg)
        await interrupt_child(
            child,
            config=config,
            runtime_paths=self.runtime_paths,
            reason="Background subagent execution was cancelled or interrupted; its tools were not replayed.",
        )

    def _authorized(self, job: BackgroundJob) -> bool:
        """Recheck current delegation, team membership, and requester reply access."""
        config = self.config_provider()
        owner = job.owner
        if config is None or owner.channel != "matrix" or owner.requester_id is None or owner.room_id is None:
            return False
        caller = config.agents.get(owner.agent_name)
        if caller is None or job.child.child_agent_name not in caller.delegate_to:
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
            for entity_name in {owner.agent_name, job.child.child_agent_name, recipient}
        )

    async def sync(self) -> None:
        """Recover once, publish the service, and wake it after config changes."""
        if self.config_provider() is None:
            await self.stop()
            return
        if self._task is None:
            await self.runtime.recover()
            register_background_runtime(self.runtime_paths, self.runtime)
            self._task = asyncio.create_task(self._run(), name="subagent_completion_worker")
        self.runtime.changed.set()

    async def stop(self) -> None:
        """Withdraw admission and stop delivery before cancelling owned child work."""
        register_background_runtime(self.runtime_paths, None)
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
                logger.exception("Background subagent completion delivery failed", job_id=job.job_id)

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
            transaction_id=f"subagent_{job.job_id}_{job.generation}",
        )
        if delivery is None or delivery.acknowledged:
            return
        # A replaced Matrix account cannot reuse another account's transaction scope.
        if delivery.content.get("m.mentions") != {"user_ids": [recipient_user_id]}:
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
