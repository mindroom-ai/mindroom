"""
Custom Synapse module to limit edit history retention.

This module automatically purges old edit events to prevent database bloat
from streaming message updates.

To use this module, add to your Synapse homeserver.yaml:

modules:
  - module: limit_edit_history.LimitEditHistoryModule
    config:
      max_edits_per_message: 5
      cleanup_interval_seconds: 3600
"""

import logging
from typing import Any

from synapse.module_api import EventBase, ModuleApi
from synapse.module_api.errors import ConfigError

logger = logging.getLogger(__name__)


class LimitEditHistoryModule:
    def __init__(self, config: dict[str, Any], api: ModuleApi):
        self._api = api
        self._max_edits = config.get("max_edits_per_message", 5)
        self._cleanup_interval = config.get("cleanup_interval_seconds", 3600)

        # Register callback for new events
        api.register_third_party_rules_callbacks(
            on_new_event=self.on_new_event,
        )

        # Schedule periodic cleanup
        api.looping_call(self._cleanup_old_edits, self._cleanup_interval * 1000)

        logger.info(
            f"LimitEditHistoryModule initialized: max_edits={self._max_edits}, "
            f"cleanup_interval={self._cleanup_interval}s"
        )

    async def on_new_event(self, event: EventBase, state_events: Any) -> None:
        """Check new events and limit edit history if needed."""
        # Only process message edits
        if event.type != "m.room.message":
            return

        relates_to = event.content.get("m.relates_to", {})
        if relates_to.get("rel_type") != "m.replace":
            return

        # Get the original event being edited
        original_event_id = relates_to.get("event_id")
        if not original_event_id:
            return

        # Count existing edits for this message
        room_id = event.room_id

        # This would need to be implemented with actual database queries
        # For now, this is a conceptual example
        await self._limit_edits_for_message(room_id, original_event_id)

    async def _limit_edits_for_message(self, room_id: str, event_id: str) -> None:
        """Limit the number of edits kept for a specific message."""
        # This would need actual implementation with database access
        # Conceptually:
        # 1. Query all edits for this message
        # 2. If count > max_edits, delete oldest ones
        # 3. Keep only the most recent max_edits
        pass

    async def _cleanup_old_edits(self) -> None:
        """Periodic cleanup of old edit events."""
        logger.info("Running periodic edit history cleanup")

        # This would need actual implementation
        # Conceptually:
        # 1. Find all messages with excessive edits
        # 2. Delete old edits keeping only recent ones
        # 3. Clean up related database tables
        pass


def parse_config(config: dict[str, Any]) -> dict[str, Any]:
    """Parse and validate module configuration."""
    max_edits = config.get("max_edits_per_message", 5)
    if not isinstance(max_edits, int) or max_edits < 1:
        raise ConfigError("max_edits_per_message must be a positive integer")

    cleanup_interval = config.get("cleanup_interval_seconds", 3600)
    if not isinstance(cleanup_interval, int) or cleanup_interval < 60:
        raise ConfigError("cleanup_interval_seconds must be at least 60")

    return config
