"""Run each agent's enabled automations on their cron schedules.

A shared agent's automations maintain its one workspace; a private agent's run once for each requester whose private
instance the primary recorded an owner for, in that requester's personal room and as that requester.
At a due time the automation's check runs off the event loop; when it returns a prompt, the agent posts it in its
room as a hook-dispatched message, like a todo poke, so the agent answers it with a normal visible run.
When that run's response is final, or after an hour without one, the prompt's continuation runs off the loop and
returns the next prompt or the notice that ends the chain.
The automations of one agent, or of one private instance, do not fire while one of their chains is active.
The runner persists only the threads it starts, so their exports are not read back as conversations; the cron cadence
is the cooldown, and a restart only skips the occurrence it missed.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from croniter import croniter

from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.automations.steps import AUTOMATION_HOOK_PREFIX, Ask, AutomationContext, Done
from mindroom.automations.threads import automations_tracking_root, record_automation_thread
from mindroom.background_tasks import create_background_task
from mindroom.config.automations import PluginAutomation
from mindroom.constants import ORIGINAL_SENDER_KEY, PER_FIRE_THREAD_ROOT_KEY, SCHEDULED_MODEL_KEY, primary_records_dir
from mindroom.entity_resolution import mindroom_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.personal_room_store import personal_room_record_path, read_personal_room
from mindroom.matrix.state import resolve_room_id
from mindroom.private_storage.private_instance_identity import private_instances_for_agent
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagsError, set_thread_tag
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.automations.registry import AutomationDefinition
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.automations import Automation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

# One agent's state that automations maintain: the agent and, for a private agent, the requester whose instance it is.
type _Instance = tuple[str, str | None]

logger = get_logger(__name__)

_RUN_FALLBACK = timedelta(hours=1)
_MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class _Chain:
    """One automation's run of prompts for one agent or private instance, from its check until its last step."""

    key: str
    agent_name: str
    # The requester whose private instance the chain maintains, or None for a shared agent.
    requester_id: str | None
    settings: Automation
    room_id: str

    @property
    def instance(self) -> _Instance:
        """Return the agent or private instance whose automations this chain holds."""
        return (self.agent_name, self.requester_id)

    @property
    def hook_source(self) -> str:
        """Return the hook source the chain's messages carry, which marks the turns they start as automation turns."""
        return f"{AUTOMATION_HOOK_PREFIX}{self.settings.name}"


@dataclass(frozen=True)
class _PendingRun:
    """A posted prompt whose run has not ended yet."""

    chain: _Chain
    thread_id: str
    ask: Ask
    deadline: datetime


def _next_time(cron: str, now: datetime, timezone: str) -> datetime:
    local_now = now.astimezone(ZoneInfo(timezone))
    return croniter(cron, local_now).get_next(datetime).astimezone(UTC)


def _key(instance: _Instance, automation_name: str) -> str:
    agent_name, requester_id = instance
    key = f"{agent_name}:{automation_name}"
    return key if requester_id is None else f"{key}:{requester_id}"


def _instances(config: Config, runtime_paths: RuntimePaths) -> list[_Instance]:
    """Return what every agent with automations maintains: a shared agent's own state, or each private instance.

    Only the primary's copy of an owner record names a requester, so an instance worker code created gets no runs.
    A requester now aliased to another ID is skipped, because ingress would run the prompt against that ID's instance.
    """
    instances: list[_Instance] = []
    for agent_name, agent in config.agents.items():
        if not config.resolve_entity(agent_name).automations:
            continue
        if agent.private is None:
            instances.append((agent_name, None))
            continue
        try:
            records = private_instances_for_agent(runtime_paths.storage_root, agent_name, agent.private.per)
        except OSError as exc:
            # Instances can be created or removed while they are listed; the next pass lists them again.
            logger.warning("Automation could not list private instances", agent=agent_name, error=str(exc))
            continue
        requesters = {
            record.requester_id
            for record in records
            if record.requester_id is not None
            and resolve_human_requester_alias(record.requester_id, config, runtime_paths) == record.requester_id
        }
        instances.extend((agent_name, requester_id) for requester_id in sorted(requesters))
    return instances


def _room_id(
    config: Config,
    runtime_paths: RuntimePaths,
    instance: _Instance,
    room: str | None,
) -> str | None:
    """Return where the instance's prompts go: the configured room, or the requester's personal room with the agent."""
    agent_name, requester_id = instance
    if requester_id is not None:
        # Replies quote the requester's private files, so they go to the requester's personal room, never a shared one.
        record = read_personal_room(personal_room_record_path(runtime_paths, agent_name, requester_id))
        return record.room_id if record is not None else None
    rooms = config.get_agent(agent_name).rooms
    room_key = room if room is not None else (rooms[0] if rooms else None)
    if room_key is None:
        return None
    room_id = resolve_room_id(room_key, runtime_paths)
    return room_id if room_id.startswith("!") else None


def _state_dir(runtime_paths: RuntimePaths, chain: _Chain, state_root: Path) -> Path:
    if chain.requester_id is None:
        return automations_tracking_root(runtime_paths) / chain.agent_name
    # Keyed by the instance's canonical state root, below the tracking directory that no worker mounts.
    return primary_records_dir(state_root, runtime_paths) / "automations"


def _check(
    config: Config,
    runtime_paths: RuntimePaths,
    chain: _Chain,
    definition: AutomationDefinition,
) -> Ask | None:
    """Run one automation's check; raises ``OSError`` or ``ValueError`` when it cannot read what it checks."""
    agent_name, automation = chain.agent_name, chain.settings
    if definition.requires_file_memory and config.resolve_entity(agent_name).memory_backend != "file":
        logger.warning("Automation needs file memory; skipped", agent=agent_name, automation=automation.name)
        return None
    # The same identity the prompt's turn resolves, so the check and the run work on the same files.
    identity = (
        None
        if chain.requester_id is None
        else build_requester_execution_identity(
            agent_name=agent_name,
            runtime_paths=runtime_paths,
            requester_id=chain.requester_id,
        )
    )
    runtime = resolve_agent_runtime(agent_name, config, runtime_paths, execution_identity=identity)
    context = AutomationContext(
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        entry=automation,
        # Each check gets its own copies, so nothing it changes reaches the config or a later check.
        options=MappingProxyType(deepcopy(automation.options) if isinstance(automation, PluginAutomation) else {}),
        settings=MappingProxyType(deepcopy(dict(definition.settings))),
        requester_id=chain.requester_id,
        execution_identity=identity,
        workspace=runtime.workspace.root if runtime.workspace is not None else None,
        state_dir=_state_dir(runtime_paths, chain, runtime.state_root),
    )
    return definition.check(context)


@dataclass
class AutomationRunner:
    """Own the automation schedule loop, the prompts it posts, and the steps that follow their runs."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], AgentBot | TeamBot | None]
    # The automations the active plugin snapshot provides, beside the built-ins, by name.
    definition_provider: Callable[[str], AutomationDefinition | None]
    # Who may use which agent, which a private instance's requester must still pass before a run on their behalf.
    agent_reply_memberships: AgentReplyMembershipIndex
    # Each automation's (cron, timezone) and the next time it is due, recomputed when either changes.
    _next_due: dict[str, tuple[tuple[str, str], datetime]] = field(default_factory=dict, init=False)
    # Agents and private instances with an automation between its check and the end of its chain; their automations
    # never overlap, because each one's run can change the memory files the other measures or applies to.
    _active: set[_Instance] = field(default_factory=set, init=False)
    _pending: dict[str, _PendingRun] = field(default_factory=dict, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def start(self) -> None:
        """Start the schedule loop once."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="automation_runner")

    async def stop(self) -> None:
        """Stop the schedule loop; prompts already posted keep their state until the process exits."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def response_finished(self, source_event_ids: Sequence[str]) -> None:
        """Continue an automation once the response its prompt started is final."""
        for event_id in source_event_ids:
            if (pending := self._pending.pop(event_id, None)) is not None:
                self._continue(pending, timed_out=False)

    async def _tick(self, now: datetime) -> None:
        """Fire every automation that is due at ``now`` and continue prompts whose run never reported back."""
        config = self.config_provider()
        if config is None:
            return
        instances = await asyncio.to_thread(_instances, config, self.runtime_paths)
        enabled: set[str] = set()
        # ``now`` is the loop's clock; prompts measure their hour on it from the moment they are delivered.
        clock_offset = now - datetime.now(UTC)
        for instance in instances:
            agent_name, _requester_id = instance
            for automation in config.resolve_entity(agent_name).automations:
                key = _key(instance, automation.name)
                enabled.add(key)
                schedule = (automation.cron, config.timezone)
                scheduled, due = self._next_due.get(key, (("", ""), now))
                if scheduled != schedule:
                    self._next_due[key] = (schedule, _next_time(automation.cron, now, config.timezone))
                elif due <= now and instance not in self._active:
                    self._next_due[key] = (schedule, _next_time(automation.cron, now, config.timezone))
                    self._active.add(instance)
                    create_background_task(
                        self._fire(config, instance, automation, key, clock_offset),
                        name=f"automation:{key}",
                    )
        for key in set(self._next_due) - enabled:
            del self._next_due[key]
        for event_id, pending in list(self._pending.items()):
            if pending.deadline <= now:
                del self._pending[event_id]
                self._continue(pending, timed_out=True)

    def _continue(self, pending: _PendingRun, *, timed_out: bool) -> None:
        create_background_task(self._advance(pending, timed_out), name=f"automation_step:{pending.chain.key}")

    def _release(self, instance: _Instance) -> None:
        self._active.discard(instance)
        # The instance is free again, so an automation held while a chain was active may be due now.
        self._wake.set()

    async def _run(self) -> None:
        while True:
            now = datetime.now(UTC)
            # Cleared before ticking, so a chain the tick ends still wakes the next pass.
            self._wake.clear()
            await self._tick(now)
            deadlines = [due for _schedule, due in self._next_due.values()] + [
                p.deadline for p in self._pending.values()
            ]
            # A held automation keeps its past due time; ending its chain wakes the loop instead.
            sleep = min([_MAX_SLEEP_SECONDS, *((d - now).total_seconds() for d in deadlines if d > now)])
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(sleep, 1.0))
            except TimeoutError:
                continue

    async def _fire(
        self,
        config: Config,
        instance: _Instance,
        automation: Automation,
        key: str,
        clock_offset: timedelta,
    ) -> None:
        agent_name, requester_id = instance
        posted = False
        try:
            room_id = await asyncio.to_thread(_room_id, config, self.runtime_paths, instance, automation.room)
            if room_id is None and requester_id is not None:
                # A requester who never got a personal room is a normal state, not a misconfiguration.
                logger.info(
                    "Automation requester has no personal room; skipped",
                    agent=agent_name,
                    automation=automation.name,
                    requester=requester_id,
                )
                return
            if room_id is None:
                logger.warning("Automation has no room to post in", agent=agent_name, automation=automation.name)
                return
            # Unresolved membership counts as no, so a run on the requester's behalf waits for a proven grant.
            if requester_id is not None and not is_sender_allowed_for_agent_reply_in_room(
                requester_id,
                agent_name,
                config,
                room_id,
                self.runtime_paths,
                self.agent_reply_memberships,
            ):
                logger.info(
                    "Automation requester may no longer use the agent; skipped",
                    agent=agent_name,
                    automation=automation.name,
                    requester=requester_id,
                )
                return
            bot = self.bot_provider(agent_name)
            if bot is None:
                logger.warning(
                    "Automation agent is not running",
                    agent=agent_name,
                    automation=automation.name,
                    requester=requester_id,
                )
                return
            if (definition := self.definition_provider(automation.name)) is None:
                logger.warning(
                    "Automation is not provided by any loaded plugin",
                    agent=agent_name,
                    automation=automation.name,
                    requester=requester_id,
                )
                return
            chain = _Chain(key, agent_name, requester_id, automation, room_id)
            try:
                step = await asyncio.to_thread(_check, config, self.runtime_paths, chain, definition)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Automation check failed",
                    agent=agent_name,
                    automation=automation.name,
                    requester=requester_id,
                    error=str(exc),
                )
                await self._notify(bot, chain, None, f"⚠️ The {automation.name} automation could not run: {exc}")
                return
            if step is not None:
                posted = await self._post(config, bot, chain, None, step, clock_offset)
        finally:
            if not posted:
                self._release(instance)

    async def _advance(self, pending: _PendingRun, timed_out: bool) -> None:
        """Run the continuation of a prompt whose run ended, then post its next prompt or its notice."""
        chain = pending.chain
        posted = False
        try:
            config = self.config_provider()
            bot = self.bot_provider(chain.agent_name)
            if pending.ask.then is None or config is None or bot is None:
                return
            name = chain.settings.name
            try:
                step = await asyncio.to_thread(pending.ask.then, config, pending.thread_id, timed_out)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Automation step failed",
                    agent=chain.agent_name,
                    automation=name,
                    requester=chain.requester_id,
                    error=str(exc),
                )
                await self._notify(bot, chain, pending.thread_id, f"⚠️ The {name} automation could not finish: {exc}")
                return
            if isinstance(step, Done):
                if step.on_loop is not None:
                    step.on_loop()
                await self._notify(bot, chain, pending.thread_id, step.notice)
                for thread_id in step.resolve:
                    await self._resolve_thread(bot, chain, thread_id)
                return
            posted = await self._post(config, bot, chain, pending.thread_id, step, timedelta(0))
        finally:
            if not posted:
                self._release(chain.instance)

    async def _notify(self, bot: AgentBot | TeamBot, chain: _Chain, thread_id: str | None, text: str) -> None:
        """Post ``text`` without mentioning the agent, so it starts no run."""
        await bot._hook_send_message(chain.room_id, text, thread_id, chain.hook_source)

    async def _post(
        self,
        config: Config,
        bot: AgentBot | TeamBot,
        chain: _Chain,
        thread_id: str | None,
        ask: Ask,
        clock_offset: timedelta,
    ) -> bool:
        """Post ``ask`` and wait for its run; return whether it was posted."""
        target_thread = None if ask.new_thread else thread_id
        event_id = await self._post_mention(config, bot, chain, ask.text, target_thread)
        if event_id is None:
            return False
        thread = event_id if target_thread is None else target_thread
        # The run's hour starts once the prompt is delivered, however long the wait, the check, or the send took.
        delivered = datetime.now(UTC) + clock_offset
        self._pending[event_id] = _PendingRun(chain, thread, ask, delivered + _RUN_FALLBACK)
        self._wake.set()
        if target_thread is None:
            try:
                await asyncio.to_thread(record_automation_thread, self.runtime_paths, event_id)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Automation could not record its thread",
                    agent=chain.agent_name,
                    requester=chain.requester_id,
                    error=str(exc),
                )
        logger.info(
            "Automation prompt posted",
            agent=chain.agent_name,
            automation=chain.settings.name,
            requester=chain.requester_id,
            new_thread=ask.new_thread,
        )
        return True

    async def _resolve_thread(self, bot: AgentBot | TeamBot, chain: _Chain, thread_id: str) -> None:
        """Mark a finished chain's thread as resolved."""
        if bot.client is None:
            return
        try:
            await set_thread_tag(
                bot.client,
                chain.room_id,
                thread_id,
                RESOLVED_THREAD_TAG,
                set_by=bot.client.user_id,
            )
        except ThreadTagsError as exc:
            logger.warning(
                "Automation could not resolve its thread",
                agent=chain.agent_name,
                requester=chain.requester_id,
                error=str(exc),
            )

    async def _post_mention(
        self,
        config: Config,
        bot: AgentBot | TeamBot,
        chain: _Chain,
        text: str,
        thread_id: str | None,
    ) -> str | None:
        """Post the prompt mentioning the agent so it answers with a normal run, and return the event ID."""
        # The prompt owns its thread, or answers in its chain's thread, and the session that goes with it, even for an
        # agent in room thread mode.
        extra_content: dict[str, str | bool] = {PER_FIRE_THREAD_ROOT_KEY: True}
        # A private instance's message runs as its requester, so the turn resolves their files and applies their access
        # and budget; a shared agent's runs as MindRoom's internal user, like a todo poke without a human requester.
        original_sender = chain.requester_id or mindroom_user_id(config, self.runtime_paths)
        if original_sender is not None:
            extra_content[ORIGINAL_SENDER_KEY] = original_sender
        if chain.settings.model is not None:
            extra_content[SCHEDULED_MODEL_KEY] = chain.settings.model
        return await bot._hook_send_message(
            chain.room_id,
            # Only a mentioned agent answers a message in a room with other responders.
            f"@{chain.agent_name} {text}",
            thread_id,
            chain.hook_source,
            extra_content,
            trigger_dispatch=True,
        )
