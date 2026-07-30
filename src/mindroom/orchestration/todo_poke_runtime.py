"""Native todo auto-poke runtime binding for the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.custom_tools.todo_poke import TodoPokeDeps, TodoPokeWorker, todo_poke_policy
from mindroom.custom_tools.todo_state import state_root as todo_state_root
from mindroom.entity_resolution import mindroom_user_id
from mindroom.scheduling import get_pending_schedule_thread_ids_for_room

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass
class TodoPokeRuntimeCoordinator:
    """Own the todo poke worker lifecycle and its orchestrator-facing adapters."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], AgentBot | TeamBot | None]
    _worker: TodoPokeWorker | None = field(default=None, init=False)
    _task: asyncio.Task | None = field(default=None, init=False)

    async def sync(self) -> None:
        """Start or stop the todo poke worker from runtime env policy."""
        policy = todo_poke_policy(self.runtime_paths)
        if self.config_provider() is None or policy.interval_seconds == 0:
            await self.stop()
            return

        if self._task is not None and not self._task.done():
            return

        worker = TodoPokeWorker(
            policy=policy,
            deps=TodoPokeDeps(
                state_root=todo_state_root(self.runtime_paths),
                schedule_query=self._schedule_query,
                idle_check=self._agent_is_idle,
                sender=self._send_poke,
                clock=lambda: datetime.now(UTC),
            ),
        )
        self._worker = worker
        self._task = asyncio.create_task(worker.run(), name="todo_poke_worker")

    async def stop(self) -> None:
        """Stop the todo poke worker if running."""
        worker = self._worker
        task = self._task
        self._worker = None
        self._task = None

        if worker is not None:
            worker.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def _agent_is_idle(self, agent_name: str) -> bool:
        """Return whether an agent and its running configured teams are idle."""
        config = self.config_provider()
        if config is None or agent_name not in config.agents:
            return False

        agent_bot = self.bot_provider(agent_name)
        if agent_bot is None or not agent_bot.running or agent_bot.in_flight_response_count != 0:
            return False

        for team_name, team_config in config.teams.items():
            if agent_name not in team_config.agents:
                continue
            team_bot = self.bot_provider(team_name)
            if team_bot is not None and team_bot.running and team_bot.in_flight_response_count != 0:
                return False
        return True

    def _router(self) -> AgentBot | TeamBot | None:
        """Return the running router bot when todo poke I/O is ready."""
        router_bot = self.bot_provider(ROUTER_AGENT_NAME)
        if router_bot is None or not router_bot.running or router_bot.client is None:
            return None
        return router_bot

    async def _schedule_query(self, room_id: str) -> frozenset[str | None] | None:
        """Return pending schedule scopes, or None while router I/O is unavailable."""
        router_bot = self._router()
        if router_bot is None:
            return None
        client = router_bot.client
        if client is None:
            return None
        return await get_pending_schedule_thread_ids_for_room(client, room_id)

    async def _send_poke(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
    ) -> str | None:
        """Send one router-originated todo poke that enters normal dispatch."""
        router_bot = self._router()
        config = self.config_provider()
        if router_bot is None or config is None:
            return None

        original_sender = mindroom_user_id(config, self.runtime_paths)
        extra_content = {ORIGINAL_SENDER_KEY: original_sender} if original_sender is not None else None
        return await router_bot._hook_send_message(
            room_id,
            body,
            thread_id,
            "todo_poke",
            extra_content,
            trigger_dispatch=True,
        )
