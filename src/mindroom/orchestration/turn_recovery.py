"""Replay the turn-journal work each bot left pending once the bots its turns need are ready."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger
from mindroom.orchestration.config_updates import configured_entity_names
from mindroom.orchestration.runtime import run_with_retry

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config

logger = get_logger(__name__)


@dataclass
class TurnJournalRecovery:
    """Own when each bot replays turn-backed journal work, which needs the agents its turns reach to have synced.

    Readiness is evaluated at three moments because each answers a different one: a ready signal releases newly ready
    bots at once, even while an earlier pass is still draining, so their replay does not wait behind it; a pass
    releases its ready set before draining any of it; and each drain resolves its entity's current generation again,
    because a fleet replacement during the previous drain's awaits can retire the generation the pass released.
    """

    config: Callable[[], Config]
    bots: Callable[[], Mapping[str, AgentBot]]
    first_sync_complete: Callable[[str], bool | None]
    runtime_ready: Callable[[], bool]
    # Owns the coalesced recovery task, so shutdown can wait for it alone.
    task_owner: object = field(default_factory=object, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _requested: bool = field(default=False, init=False, repr=False)

    def schedule(self) -> None:
        """Release every ready bot's replay now, and coalesce one background pass that drains them."""
        if not self.runtime_ready():
            return
        self._release_ready_bots()
        self._requested = True
        if self._task is not None and not self._task.done():
            return
        self._task = create_background_task(
            self._run_requested_passes(),
            name="recover_ready_turn_journal_events",
            owner=self.task_owner,
        )

    async def recover(self) -> None:
        """Drain the turn-journal work of every entity whose required bots are ready, raising the first failure."""
        async with self._lock:
            first_error: Exception | None = None
            for entity_name in self._release_ready_bots():
                error = await self._drain_current_generation(entity_name)
                first_error = first_error or error
            if first_error is not None:
                raise first_error

    def _release_ready_bots(self) -> dict[str, AgentBot]:
        """Release and return each configured entity's current bot whose required bots completed their first sync."""
        config = self.config()
        ready = {
            entity_name: bot
            for entity_name in configured_entity_names(config)
            if (bot := self._ready_bot(entity_name, config)) is not None
        }
        for bot in ready.values():
            bot.release_pending_turn_journal_replay()
        return ready

    def _ready_bot(self, entity_name: str, config: Config) -> AgentBot | None:
        """Return the entity's current bot once it and, for a team, every member completed their first sync."""
        bot = self.bots().get(entity_name)
        required = [entity_name, *config.teams[entity_name].agents] if entity_name in config.teams else [entity_name]
        if bot is None or any(self.first_sync_complete(name) is not True for name in required):
            return None
        return bot

    async def _drain_current_generation(self, entity_name: str) -> Exception | None:
        """Drain the entity's current generation if it is still configured and ready, returning its failure."""
        config = self.config()
        if entity_name not in configured_entity_names(config):
            return None
        bot = self._ready_bot(entity_name, config)
        if bot is None:
            return None
        try:
            await bot.recover_pending_turn_journal_events()
        except Exception as error:
            logger.exception("turn_dispatch_recovery_failed", agent_name=entity_name)
            return error
        return None

    async def _run_requested_passes(self) -> None:
        """Run passes until no ready signal arrived during the last one, without blocking a Matrix sync callback."""

        async def recover_while_ready() -> None:
            if self.runtime_ready():
                await self.recover()

        current_task = asyncio.current_task()
        try:
            while self._requested:
                self._requested = False
                await run_with_retry(
                    "Recovering ready turn dispatch obligations",
                    recover_while_ready,
                    update_runtime_state=False,
                )
        finally:
            if self._task is current_task:
                self._task = None
