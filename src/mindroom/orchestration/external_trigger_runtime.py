"""External trigger runtime binding for the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import get_joined_rooms
from mindroom.matrix.state import resolve_room_aliases

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass
class ExternalTriggerRuntimeCoordinator:
    """Own external trigger API runtime binding and live deliverability state."""

    runtime_paths: RuntimePaths
    config_getter: Callable[[], Config | None]
    bots_getter: Callable[[], Mapping[str, AgentBot | TeamBot]]
    api_enabled: bool = True
    joined_room_ids: dict[str, frozenset[str]] = field(default_factory=dict)

    def _bind_from_started_bots(
        self,
        bots: Iterable[AgentBot | TeamBot],
        *,
        ready_trigger_ids: frozenset[str],
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
                ready_trigger_ids=ready_trigger_ids,
                is_trigger_ready=self.is_ready,
            )
            return

    def ready_trigger_ids(self, config: Config) -> frozenset[str]:
        """Return enabled triggers whose target bot is joined to that trigger room."""
        ready_trigger_ids = set()
        bots = self.bots_getter()
        router_joined_room_ids = self.joined_room_ids.get(ROUTER_AGENT_NAME, frozenset())
        for trigger_id, trigger_config in config.external_triggers.items():
            if not trigger_config.enabled:
                continue
            target_bot = bots.get(trigger_config.target.agent)
            if target_bot is None or not target_bot.running or target_bot.client is None:
                continue
            target_room_id = self.resolve_room_id(trigger_config.target.room_id)
            target_joined_room_ids = self.joined_room_ids.get(
                trigger_config.target.agent,
                frozenset(),
            )
            if target_room_id in router_joined_room_ids and target_room_id in target_joined_room_ids:
                ready_trigger_ids.add(trigger_id)
        return frozenset(ready_trigger_ids)

    def resolve_room_id(self, room_id_or_alias: str) -> str:
        """Resolve one configured external trigger room reference when known."""
        resolved = resolve_room_aliases([room_id_or_alias], runtime_paths=self.runtime_paths)
        return resolved[0] if resolved else room_id_or_alias

    def bind_if_ready(self) -> None:
        """Bind trigger delivery runtime after router is running."""
        if not self.api_enabled:
            return
        config = self.config_getter()
        if config is None:
            return
        router_bot = self.bots_getter().get(ROUTER_AGENT_NAME)
        if router_bot is None or not router_bot.running:
            return
        self._bind_from_started_bots(
            (router_bot,),
            ready_trigger_ids=self.ready_trigger_ids(config),
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
        configs_to_check = configs or (self.config_getter(),)
        for config in configs_to_check:
            if config is None:
                continue
            for trigger_config in config.external_triggers.values():
                if trigger_config.enabled and trigger_config.target.agent in affected_entities:
                    self.unbind()
                    for entity_name in affected_entities:
                        self.joined_room_ids.pop(entity_name, None)
                    return

    async def refresh_joined_room_ids(self, bots: Iterable[AgentBot | TeamBot]) -> None:
        """Refresh actual Matrix joined-room snapshots for trigger delivery participants."""
        config = self.config_getter()
        if config is None:
            return
        trigger_target_agents = {
            trigger_config.target.agent
            for trigger_config in config.external_triggers.values()
            if trigger_config.enabled
        }
        if not trigger_target_agents:
            return
        trigger_participant_agents = {ROUTER_AGENT_NAME, *trigger_target_agents}
        bots_to_refresh = list(bots)
        refreshed_entity_names = {bot.agent_name for bot in bots_to_refresh}
        router_bot = self.bots_getter().get(ROUTER_AGENT_NAME)
        if ROUTER_AGENT_NAME not in refreshed_entity_names and router_bot is not None:
            bots_to_refresh.append(router_bot)
        for bot in bots_to_refresh:
            if bot.agent_name not in trigger_participant_agents:
                continue
            joined_rooms = await get_joined_rooms(bot.client) if bot.client is not None else None
            self.joined_room_ids[bot.agent_name] = frozenset(joined_rooms or ())

    async def is_ready(self, trigger_id: str) -> bool:
        """Return whether router and target clients are currently joined to one trigger room."""
        config = self.config_getter()
        if config is None:
            return False
        trigger_config = config.external_triggers.get(trigger_id)
        if trigger_config is None or not trigger_config.enabled:
            return False
        bots = self.bots_getter()
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
        self.joined_room_ids[ROUTER_AGENT_NAME] = router_joined_room_ids
        self.joined_room_ids[trigger_config.target.agent] = target_joined_room_ids
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
