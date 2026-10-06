"""Run each agent's enabled automations on their cron schedules.

At a due time the automation's check runs off the event loop; when it passes, the agent posts the automation's prompt
in its room as a hook-dispatched message, like a todo poke, so the agent answers it with a normal visible run.
When that run's response is final, or after an hour without one, the verify step runs and its notice is posted in the
prompt's thread.
Nothing is persisted: the cron cadence is the cooldown, and a restart only skips the occurrence it missed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from croniter import croniter

from mindroom.automations.prompt_curation import curation_notice, curation_prompt, plan_curation, verify_curation
from mindroom.background_tasks import create_background_task
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.entity_resolution import mindroom_user_id
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_id

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.automations.prompt_curation import CurationPlan
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.automations import PromptCurationAutomation
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_SOURCE_HOOK = "prompt_curation"
_VERIFY_FALLBACK = timedelta(hours=1)
_MAX_SLEEP_SECONDS = 60.0


@dataclass(frozen=True)
class _PendingVerify:
    """A posted prompt whose run has not ended yet."""

    key: str
    room_id: str
    thread_id: str
    plan: CurationPlan
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


@dataclass
class AutomationRunner:
    """Own the automation schedule loop, the prompts it posts, and the verify steps that follow them."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], AgentBot | TeamBot | None]
    # Each automation's (cron, timezone) and the next time it is due, recomputed when either changes.
    _next_due: dict[str, tuple[tuple[str, str], datetime]] = field(default_factory=dict, init=False)
    _firing: set[str] = field(default_factory=set, init=False)
    _verifying: set[str] = field(default_factory=set, init=False)
    _pending: dict[str, _PendingVerify] = field(default_factory=dict, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def start(self) -> None:
        """Start the schedule loop once."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="automation_runner")

    async def stop(self) -> None:
        """Stop the schedule loop; prompts already posted keep their snapshots until the process exits."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def response_finished(self, source_event_ids: Sequence[str]) -> None:
        """Verify an automation prompt once the response it started is final."""
        for event_id in source_event_ids:
            if (pending := self._pending.pop(event_id, None)) is not None:
                self._start_verify(pending)

    async def _tick(self, now: datetime) -> None:
        """Fire every automation that is due at ``now`` and verify prompts whose run never reported back."""
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
                elif due <= now and not self._busy(key):
                    self._next_due[key] = (schedule, _next_time(automation.cron, now, config.timezone))
                    self._firing.add(key)
                    create_background_task(
                        self._fire(config, agent_name, automation, key, now),
                        name=f"automation:{key}",
                    )
        for key in set(self._next_due) - enabled:
            del self._next_due[key]
        for event_id, pending in list(self._pending.items()):
            if pending.deadline <= now:
                del self._pending[event_id]
                self._start_verify(pending)

    def _start_verify(self, pending: _PendingVerify) -> None:
        # The automation stays busy until verify ends, so a new pass never snapshots files a restore is replacing.
        self._verifying.add(pending.key)
        create_background_task(self._verify(pending), name=f"automation_verify:{pending.plan.agent_name}")

    def _busy(self, key: str) -> bool:
        """Return whether this automation is checking, waiting on its last prompt's run, or verifying it."""
        return (
            key in self._firing
            or key in self._verifying
            or any(pending.key == key for pending in self._pending.values())
        )

    async def _run(self) -> None:
        while True:
            now = datetime.now(UTC)
            await self._tick(now)
            deadlines = [due for _schedule, due in self._next_due.values()] + [
                p.deadline for p in self._pending.values()
            ]
            # A held automation keeps its past due time; response_finished wakes the loop instead of a busy poll.
            sleep = min([_MAX_SLEEP_SECONDS, *((d - now).total_seconds() for d in deadlines if d > now)])
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(sleep, 1.0))
            except TimeoutError:
                continue

    async def _fire(
        self,
        config: Config,
        agent_name: str,
        automation: PromptCurationAutomation,
        key: str,
        now: datetime,
    ) -> None:
        try:
            plan = await asyncio.to_thread(plan_curation, config, self.runtime_paths, agent_name, automation)
            if plan is None:
                return
            room_id = _room_id(config, self.runtime_paths, agent_name, automation.room)
            if room_id is None:
                logger.warning("Automation has no room to post in", agent=agent_name, automation=automation.name)
                return
            bot = self.bot_provider(agent_name)
            if bot is None:
                logger.warning("Automation agent is not running", agent=agent_name, automation=automation.name)
                return
            # Like a todo poke without a human requester, the prompt runs as MindRoom's internal user.
            original_sender = mindroom_user_id(config, self.runtime_paths)
            event_id = await bot._hook_send_message(
                room_id,
                # Only a mentioned agent answers a top-level message in a room with other responders.
                f"@{agent_name} {curation_prompt(config, plan)}",
                None,
                _SOURCE_HOOK,
                {ORIGINAL_SENDER_KEY: original_sender} if original_sender is not None else None,
                trigger_dispatch=True,
            )
            if event_id is not None:
                self._pending[event_id] = _PendingVerify(key, room_id, event_id, plan, now + _VERIFY_FALLBACK)
                self._wake.set()
                logger.info(
                    "Automation prompt posted",
                    agent=agent_name,
                    automation=automation.name,
                    # A field named only "tokens" is redacted as a credential.
                    file_tokens=plan.measured_tokens,
                    target_tokens=plan.upper_tokens,
                    floor_tokens=plan.floor_tokens,
                )
        except (OSError, ValueError) as exc:
            logger.warning("Automation check failed", agent=agent_name, automation=automation.name, error=str(exc))
        finally:
            self._firing.discard(key)

    async def _verify(self, pending: _PendingVerify) -> None:
        try:
            await self._verify_and_notify(pending)
        finally:
            self._verifying.discard(pending.key)
            # A held automation may be due again now.
            self._wake.set()

    async def _verify_and_notify(self, pending: _PendingVerify) -> None:
        plan = pending.plan
        result = await asyncio.to_thread(verify_curation, plan)
        logger.info(
            "Automation verified",
            agent=plan.agent_name,
            automation=plan.settings.name,
            changed=result.changed,
            restored=result.restored,
            violations=list(result.violations),
            tokens_before=plan.measured_tokens,
            tokens_after=result.tokens_after,
        )
        bot = self.bot_provider(plan.agent_name)
        if bot is not None:
            await bot._hook_send_message(
                pending.room_id,
                curation_notice(plan, result),
                pending.thread_id,
                _SOURCE_HOOK,
            )
