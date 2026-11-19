"""External trigger runtime binding for the orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.client_room_admin import get_joined_rooms

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.external_triggers.store import TriggerDeliverySnapshot


@dataclass
class ExternalTriggerRuntimeCoordinator:
    """Own external trigger API runtime binding and live deliverability state."""

    api_enabled: bool = True

    def bind_if_ready(
        self,
        config: Config,
        bots: Mapping[str, AgentBot],
    ) -> None:
        """Bind trigger delivery through each trigger's target once any bot runs."""
        if not self.api_enabled or not any(_running(bot) for bot in bots.values()):
            return

        async def is_trigger_snapshot_ready(snapshot: TriggerDeliverySnapshot) -> bool:
            return await self._is_ready(snapshot, bots)

        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.bind_external_trigger_runtime(
            api_main.app,
            runtime_config=config,
            is_trigger_snapshot_ready=is_trigger_snapshot_ready,
            target_bot=lambda entity_name: _running(bots.get(entity_name)),
        )

    def unbind(self) -> None:
        """Clear external trigger delivery runtime from the bundled API app."""
        if not self.api_enabled:
            return
        from mindroom.api import main as api_main  # noqa: PLC0415

        api_main.unbind_external_trigger_runtime(api_main.app)

    async def _is_ready(
        self,
        snapshot: TriggerDeliverySnapshot,
        bots: Mapping[str, AgentBot],
    ) -> bool:
        """Return whether the trigger's target, which posts and answers it, is currently joined to its room."""
        target_bot = _running(bots.get(snapshot.target.agent))
        if not snapshot.enabled or target_bot is None or target_bot.client is None:
            return False
        return snapshot.resolved_room_id in (await get_joined_rooms(target_bot.client) or ())


def _running(bot: AgentBot | None) -> AgentBot | None:
    """Return the bot while it runs with a client: the one test of a trigger target for binding and readiness."""
    return bot if bot is not None and bot.running and bot.client is not None else None
