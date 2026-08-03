"""Record Matrix source redactions before updating advisory cache state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class RedactedTurnCleanupDeps:
    """Collaborators needed to tombstone one redacted source."""

    conversation_cache: MatrixConversationCache
    turn_store: TurnStore
    source_for_inflight_revision_event_id: Callable[[str], str | None]
    regenerate_redacted_revision: Callable[[nio.MatrixRoom, nio.RedactionEvent, str], Awaitable[None]]


@dataclass
class RedactedTurnCleanup:
    """Own durable source tombstoning and advisory cache sanitization."""

    deps: RedactedTurnCleanupDeps

    async def handle(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Persist the tombstone, update cache state, then replay a reverted edit."""
        inflight_source_event_id = self.deps.source_for_inflight_revision_event_id(event.redacts)
        revision_source = await asyncio.to_thread(
            self.deps.turn_store.source_for_revision_event_id,
            event.redacts,
        )
        source_event_id = revision_source[1] if revision_source is not None else inflight_source_event_id
        await asyncio.to_thread(self.deps.turn_store.mark_source_redacted, event.redacts)
        await self.deps.conversation_cache.apply_redaction(room.room_id, event)
        if source_event_id is not None:
            await self.deps.regenerate_redacted_revision(room, event, source_event_id)
