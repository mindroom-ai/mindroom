"""Record Matrix source redactions before updating advisory cache state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio

    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class RedactedTurnCleanupDeps:
    """Collaborators needed to tombstone one redacted source."""

    conversation_cache: MatrixConversationCache
    turn_store: TurnStore


@dataclass
class RedactedTurnCleanup:
    """Own durable source tombstoning and advisory cache sanitization."""

    deps: RedactedTurnCleanupDeps

    async def handle(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Persist the tombstone before applying the redaction to cached history."""
        await asyncio.to_thread(self.deps.turn_store.mark_source_redacted, event.redacts)
        await self.deps.conversation_cache.apply_redaction(room.room_id, event)
