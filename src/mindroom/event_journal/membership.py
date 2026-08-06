"""Deciding when a room's derived state must stop being trusted.

Rejoining a room can expose a different slice of history than the bot saw
before, so the projection built under the old membership has to be dropped
rather than merged with the new view. `advance_membership_epoch` owns that
invalidation. This owns the harder half: deciding when to ask for it.

One departure reaches the bot twice -- once locally, the moment the bot leaves,
and again in the sync response reporting the leave. Both describe the same
departure and must fence once. Fencing twice is not merely wasteful: if the bot
rejoined in between, the second fence deletes the conversation it has already
hydrated under the new membership, along with any answer queued for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = get_logger(__name__)


class MembershipView(Protocol):
    """Advancing one room's membership epoch, and nothing else."""

    async def advance_membership_epoch(self, room_id: str) -> int:
        """Invalidate everything derived for a room under its previous membership."""
        ...


@dataclass(slots=True)
class MembershipFence:
    """Advance a room's membership epoch exactly once per departure."""

    store: MembershipView
    # Rooms fenced locally whose sync echo has not arrived yet. A rejoin does
    # not clear this. The echo is still owed, and when it comes it still
    # describes the departure that was already fenced, not a new one.
    _awaiting_echo: set[str] = field(default_factory=set)

    async def fence_local_departure(self, room_id: str) -> None:
        """Fence a room this bot has just left, ahead of the sync that reports it."""
        self._awaiting_echo.add(room_id)
        await self._advance(room_id)

    async def fence_reported_departures(self, room_ids: Iterable[str]) -> None:
        """Fence departures a sync reported, absorbing the echo of local ones."""
        for room_id in room_ids:
            if room_id in self._awaiting_echo:
                self._awaiting_echo.discard(room_id)
                continue
            await self._advance(room_id)

    async def _advance(self, room_id: str) -> None:
        epoch = await self.store.advance_membership_epoch(room_id)
        logger.info("journal_membership_fenced", room_id=room_id, membership_epoch=epoch)
