"""Run each agent's enabled automations on their cron schedules.

At a due time the automation's check runs off the event loop; when it returns a prompt, the agent posts it in its
room as a hook-dispatched message, like a todo poke, so the agent answers it with a normal visible run.
When that run's response is final, or after an hour without one, the prompt's continuation runs off the loop and
returns the next prompt or the notice that ends the chain.
An agent's automations do not fire while one of its chains is active.
The runner persists only the threads it starts, so their exports are not read back as conversations; the cron cadence
is the cooldown, and a restart only skips the occurrence it missed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from croniter import croniter

from mindroom.automations.steps import AUTOMATION_HOOK_PREFIX, Ask, AutomationContext, Done
from mindroom.automations.threads import automations_tracking_root, record_automation_thread
from mindroom.background_tasks import create_background_task
from mindroom.config.automations import PluginAutomation
from mindroom.constants import ORIGINAL_SENDER_KEY, PER_FIRE_THREAD_ROOT_KEY, SCHEDULED_MODEL_KEY
from mindroom.entity_resolution import mindroom_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_id
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagsError, set_thread_tag

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.automations.registry import AutomationDefinition
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.automations import Automation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_RUN_FALLBACK = timedelta(hours=1)
_MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class _Chain:
    """One automation's run of prompts for one agent, from its check until its last step."""

    key: str
    agent_name: str
    settings: Automation
    room_id: str

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


def _room_id(config: Config, runtime_paths: RuntimePaths, agent_name: str, room: str | None) -> str | None:
    rooms = config.get_agent(agent_name).rooms
    room_key = room if room is not None else (rooms[0] if rooms else None)
    if room_key is None:
        return None
    room_id = resolve_room_id(room_key, runtime_paths)
    return room_id if room_id.startswith("!") else None


def _check(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    automation: Automation,
    definition: AutomationDefinition,
) -> Ask | None:
    """Run one automation's check; raises ``OSError`` or ``ValueError`` when it cannot read what it checks."""
    if definition.requires_file_memory and config.resolve_entity(agent_name).memory_backend != "file":
        logger.warning("Automation needs file memory; skipped", agent=agent_name, automation=automation.name)
        return None
    runtime = resolve_agent_runtime(agent_name, config, runtime_paths, execution_identity=None)
    context = AutomationContext(
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        entry=automation,
        options=MappingProxyType(dict(automation.options) if isinstance(automation, PluginAutomation) else {}),
        settings=definition.settings,
        workspace=runtime.workspace.root if runtime.workspace is not None else None,
        state_dir=automations_tracking_root(runtime_paths) / agent_name,
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
    # Each automation's (cron, timezone) and the next time it is due, recomputed when either changes.
    _next_due: dict[str, tuple[tuple[str, str], datetime]] = field(default_factory=dict, init=False)
    # Agents with an automation between its check and the end of its chain; one agent's automations never overlap,
    # because each one's run can change the memory files the other measures or applies to.
    _active: set[str] = field(default_factory=set, init=False)
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
        enabled: set[str] = set()
        for agent_name in config.agents:
            for automation in config.resolve_entity(agent_name).automations:
                key = f"{agent_name}:{automation.name}"
                enabled.add(key)
                schedule = (automation.cron, config.timezone)
                scheduled, due = self._next_due.get(key, (("", ""), now))
                if scheduled != schedule:
                    self._next_due[key] = (schedule, _next_time(automation.cron, now, config.timezone))
                elif due <= now and agent_name not in self._active:
                    self._next_due[key] = (schedule, _next_time(automation.cron, now, config.timezone))
                    self._active.add(agent_name)
                    create_background_task(
                        self._fire(config, agent_name, automation, key, now),
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

    def _release(self, agent_name: str) -> None:
        self._active.discard(agent_name)
        # The agent is free again, so an automation held while a chain was active may be due now.
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
        agent_name: str,
        automation: Automation,
        key: str,
        now: datetime,
    ) -> None:
        posted = False
        started = datetime.now(UTC)
        try:
            room_id = _room_id(config, self.runtime_paths, agent_name, automation.room)
            if room_id is None:
                logger.warning("Automation has no room to post in", agent=agent_name, automation=automation.name)
                return
            bot = self.bot_provider(agent_name)
            if bot is None:
                logger.warning("Automation agent is not running", agent=agent_name, automation=automation.name)
                return
            if (definition := self.definition_provider(automation.name)) is None:
                logger.warning(
                    "Automation is not provided by any loaded plugin",
                    agent=agent_name,
                    automation=automation.name,
                )
                return
            chain = _Chain(key, agent_name, automation, room_id)
            try:
                step = await asyncio.to_thread(_check, config, self.runtime_paths, agent_name, automation, definition)
            except (OSError, ValueError) as exc:
                logger.warning("Automation check failed", agent=agent_name, automation=automation.name, error=str(exc))
                await self._notify(bot, chain, None, f"⚠️ The {automation.name} automation could not run: {exc}")
                return
            if step is not None:
                # ``now`` is the tick's clock; the check's own run time is added to it.
                posted = await self._post(config, bot, chain, None, step, now + (datetime.now(UTC) - started))
        finally:
            if not posted:
                self._release(agent_name)

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
                logger.warning("Automation step failed", agent=chain.agent_name, automation=name, error=str(exc))
                await self._notify(bot, chain, pending.thread_id, f"⚠️ The {name} automation could not finish: {exc}")
                return
            if isinstance(step, Done):
                if step.on_loop is not None:
                    step.on_loop()
                await self._notify(bot, chain, pending.thread_id, step.notice)
                for thread_id in step.resolve:
                    await self._resolve_thread(bot, chain, thread_id)
                return
            posted = await self._post(config, bot, chain, pending.thread_id, step, datetime.now(UTC))
        finally:
            if not posted:
                self._release(chain.agent_name)

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
        now: datetime,
    ) -> bool:
        """Post ``ask`` and wait for its run; return whether it was posted."""
        target_thread = None if ask.new_thread else thread_id
        send_started = datetime.now(UTC)
        event_id = await self._post_mention(config, bot, chain, ask.text, target_thread)
        if event_id is None:
            return False
        thread = event_id if target_thread is None else target_thread
        # The run's hour starts once the prompt is delivered, however long the check or the send took.
        delivered = now + (datetime.now(UTC) - send_started)
        self._pending[event_id] = _PendingRun(chain, thread, ask, delivered + _RUN_FALLBACK)
        self._wake.set()
        if target_thread is None:
            try:
                await asyncio.to_thread(record_automation_thread, self.runtime_paths, event_id)
            except (OSError, ValueError) as exc:
                logger.warning("Automation could not record its thread", agent=chain.agent_name, error=str(exc))
        logger.info(
            "Automation prompt posted",
            agent=chain.agent_name,
            automation=chain.settings.name,
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
            logger.warning("Automation could not resolve its thread", agent=chain.agent_name, error=str(exc))

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
        # Like a todo poke without a human requester, the message runs as MindRoom's internal user.
        if (original_sender := mindroom_user_id(config, self.runtime_paths)) is not None:
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
