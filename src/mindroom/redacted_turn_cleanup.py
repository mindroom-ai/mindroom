"""Record Matrix source redactions before updating advisory cache state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio

    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.terminal_delivery import TerminalDeliveryCoordinator


@dataclass(frozen=True)
class RedactedTurnCleanupDeps:
    """Collaborators needed to tombstone one redacted source."""

    conversation_cache: MatrixConversationCache
    terminal_delivery_coordinator: TerminalDeliveryCoordinator


@dataclass
class RedactedTurnCleanup:
    """Own durable source tombstoning and advisory cache sanitization."""

    deps: RedactedTurnCleanupDeps

    async def handle(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Persist the tombstone before applying the redaction to cached history."""
        try:
            await self.deps.terminal_delivery_coordinator.redact(
                room_id=room.room_id,
                event_id=event.redacts,
            )
        finally:
            await self.deps.conversation_cache.apply_redaction(room.room_id, event)
