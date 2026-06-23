"""External trigger runtime binding for the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.state import resolve_room_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass
class ExternalTriggerRuntimeCoordinator:
    """Own external trigger API runtime binding and live deliverability state."""

    runtime_paths: RuntimePaths
    api_enabled: bool = True

    def _bind_from_started_bots(
        self,
        bots: Iterable[AgentBot | TeamBot],
        *,
        is_trigger_ready: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Bind external trigger delivery runtime when the router client is ready."""
        if not self.api_enabled:
            return
        for bot in bots:
            if bot.agent_name != ROUTER_AGENT_NAME or bot.client is None:
                continue
            from mindroom.api import main as api_main  # noqa: PLC0415

            api_main.bind_external_trigger_runtime(
                api_main.app,
                client=bot.client,
                conversation_cache=bot._conversation_cache,
                is_trigger_ready=is_trigger_ready,
            )
            return

    def resolve_room_id(self, room_id_or_alias: str) -> str:
        """Resolve one configured external trigger room reference when known."""
        return resolve_room_id(room_id_or_alias, runtime_paths=self.runtime_paths)

    def bind_if_ready(
        self,
        config: Config | None,
        bots: Mapping[str, AgentBot | TeamBot],
    ) -> None:
        """Bind trigger delivery runtime after router is running."""
        if not self.api_enabled:
            return
        if config is None:
            return
        router_bot = bots.get(ROUTER_AGENT_NAME)
        if router_bot is None or not router_bot.running:
            return

        async def is_trigger_ready(trigger_id: str) -> bool:
            return await self.is_ready(trigger_id, config, bots)

        self._bind_from_started_bots(
            (router_bot,),
            is_trigger_ready=is_trigger_ready,
        )

    def unbind(self) -> None:
        """Clear external trigger delivery runtime from the bundled API app."""
        if not self.api_enabled:
            return
        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.unbind_external_trigger_runtime(api_main.app)

    def unbind_if_delivery_affected(
        self,
        entity_names: Iterable[str],
        *configs: Config | None,
    ) -> None:
        """Clear trigger runtime before the router or a configured trigger target changes."""
        affected_entities = set(entity_names)
        if not affected_entities:
            return
        if ROUTER_AGENT_NAME in affected_entities:
            self.unbind()
            return
        for config in configs:
            if config is None:
                continue
            for trigger_config in config.external_triggers.values():
                if trigger_config.enabled and trigger_config.target.agent in affected_entities:
                    self.unbind()
                    return

    async def is_ready(
        self,
        trigger_id: str,
        config: Config | None,
        bots: Mapping[str, AgentBot | TeamBot],
    ) -> bool:
        """Return whether router and target clients are currently joined to one trigger room."""
        if config is None:
            return False
        trigger_config = config.external_triggers.get(trigger_id)
        if trigger_config is None or not trigger_config.enabled:
            return False
        router_bot = bots.get(ROUTER_AGENT_NAME)
        target_bot = bots.get(trigger_config.target.agent)
        if (
            router_bot is None
            or router_bot.client is None
            or not router_bot.running
            or target_bot is None
            or target_bot.client is None
            or not target_bot.running
        ):
            return False
        trigger_room_id = self.resolve_room_id(trigger_config.target.room_id)
        router_joined_room_ids = frozenset(await get_joined_rooms(router_bot.client) or ())
        target_joined_room_ids = frozenset(await get_joined_rooms(target_bot.client) or ())
        return trigger_room_id in router_joined_room_ids and trigger_room_id in target_joined_room_ids

    async def sync_api_config_snapshot(
        self,
        current_config: Config,
        new_config: Config,
    ) -> None:
        """Publish the current config to the bundled API before binding trigger runtime."""
        if not self.api_enabled:
            return
        if not (current_config.external_triggers or new_config.external_triggers):
            return
        from mindroom.api import main as api_main  # noqa: PLC0415

        published = await asyncio.to_thread(
            api_main.config_lifecycle._publish_runtime_config_into_app,
            new_config,
            self.runtime_paths,
            api_main.app,
        )
        if not published:
            self.unbind()
            message = "Failed to publish external trigger API config snapshot"
            raise RuntimeError(message)
