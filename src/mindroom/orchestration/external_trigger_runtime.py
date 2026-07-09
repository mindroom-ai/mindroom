"""External trigger runtime binding for the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import get_joined_rooms

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.api.config_lifecycle import PreparedRuntimeConfigPublish
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import RuntimeConfig
    from mindroom.constants import RuntimePaths
    from mindroom.external_triggers.store import TriggerDeliverySnapshot


@dataclass
class ExternalTriggerRuntimeCoordinator:
    """Own external trigger API runtime binding and live deliverability state."""

    runtime_paths: RuntimePaths
    api_enabled: bool = True

    def bind_if_ready(
        self,
        config: RuntimeConfig | None,
        bots: Mapping[str, AgentBot | TeamBot],
    ) -> None:
        """Bind trigger delivery runtime after router is running."""
        if not self.api_enabled:
            return
        if config is None:
            return
        router_bot = bots.get(ROUTER_AGENT_NAME)
        if router_bot is None or router_bot.client is None or not router_bot.running:
            return

        async def is_trigger_snapshot_ready(snapshot: TriggerDeliverySnapshot) -> bool:
            return await self.is_ready(snapshot, bots)

        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.bind_external_trigger_runtime(
            api_main.app,
            client=router_bot.client,
            conversation_cache=router_bot._conversation_cache,
            is_trigger_snapshot_ready=is_trigger_snapshot_ready,
        )

    def unbind(self) -> None:
        """Clear external trigger delivery runtime from the bundled API app."""
        if not self.api_enabled:
            return
        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.unbind_external_trigger_runtime(api_main.app)

    def unbind_for_entity_changes(
        self,
        entity_names: Iterable[str],
    ) -> None:
        """Clear trigger runtime before any entity lifecycle changes."""
        affected_entities = set(entity_names)
        if not affected_entities:
            return
        self.unbind()

    async def is_ready(
        self,
        snapshot: TriggerDeliverySnapshot,
        bots: Mapping[str, AgentBot | TeamBot],
    ) -> bool:
        """Return whether router and target clients are currently joined to one trigger room."""
        if not snapshot.enabled:
            return False
        router_bot = bots.get(ROUTER_AGENT_NAME)
        target_bot = bots.get(snapshot.target.agent)
        if (
            router_bot is None
            or router_bot.client is None
            or not router_bot.running
            or target_bot is None
            or target_bot.client is None
            or not target_bot.running
        ):
            return False
        router_joined_room_ids = frozenset(await get_joined_rooms(router_bot.client) or ())
        target_joined_room_ids = frozenset(await get_joined_rooms(target_bot.client) or ())
        return (
            snapshot.resolved_room_id in router_joined_room_ids and snapshot.resolved_room_id in target_joined_room_ids
        )

    async def prepare_api_config_snapshot(
        self,
        new_config: RuntimeConfig,
    ) -> PreparedRuntimeConfigPublish | None:
        """Prepare the slow inputs for one bundled-API config publication."""
        if not self.api_enabled:
            return None
        from mindroom.api import main as api_main  # noqa: PLC0415
        from mindroom.api.config_lifecycle import prepare_runtime_config_publish  # noqa: PLC0415

        api_main.initialize_api_app(api_main.app, self.runtime_paths)
        return await asyncio.to_thread(
            prepare_runtime_config_publish,
            new_config,
            self.runtime_paths,
            api_main.app,
        )

    def publish_prepared_api_config_snapshot(
        self,
        prepared: PreparedRuntimeConfigPublish | None,
    ) -> None:
        """Publish one prepared bundled-API config snapshot without yielding."""
        if prepared is None:
            return
        from mindroom.api import main as api_main  # noqa: PLC0415
        from mindroom.api.config_lifecycle import publish_prepared_runtime_config_into_app  # noqa: PLC0415

        published = publish_prepared_runtime_config_into_app(
            prepared,
            api_main.app,
        )
        if not published:
            self.unbind()
            message = "Failed to publish external trigger API config snapshot"
            raise RuntimeError(message)

    async def sync_api_config_snapshot(
        self,
        new_config: RuntimeConfig,
    ) -> None:
        """Publish the current config to the bundled API before binding trigger runtime."""
        prepared = await self.prepare_api_config_snapshot(new_config)
        self.publish_prepared_api_config_snapshot(prepared)
