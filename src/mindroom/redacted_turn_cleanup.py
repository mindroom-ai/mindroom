"""Record Matrix source redactions before updating advisory cache state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio

    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.terminal_delivery import TerminalDeliveryCoordinator
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class RedactedTurnCleanupDeps:
    """Collaborators needed to tombstone one redacted source."""

    conversation_cache: MatrixConversationCache
    turn_store: TurnStore
    terminal_delivery_coordinator: TerminalDeliveryCoordinator


@dataclass
class RedactedTurnCleanup:
    """Own durable source tombstoning and advisory cache sanitization."""

    deps: RedactedTurnCleanupDeps

    async def handle(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Persist the tombstone before applying the redaction to cached history."""
        await asyncio.to_thread(self.deps.turn_store.mark_source_redacted, event.redacts)
        try:
            await self._cancel_pending_terminal_deliveries(room.room_id, event.redacts)
        finally:
            await self.deps.conversation_cache.apply_redaction(room.room_id, event)

    async def _cancel_pending_terminal_deliveries(self, room_id: str, redacted_event_id: str) -> None:
        """Stop durable retries whose source or visible target was just redacted."""
        await self.deps.terminal_delivery_coordinator.redact(
            room_id=room_id,
            event_id=redacted_event_id,
        )
