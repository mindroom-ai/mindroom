"""Durable ordering and visible settlement for user stop reactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class UserStopReconcilerDeps:
    """Collaborators for durable stop settlement."""

    turn_store: TurnStore
    delivery_gateway: DeliveryGateway


@dataclass
class UserStopReconciler:
    """Record one stop intent on the reply it reaches."""

    deps: UserStopReconcilerDeps

    async def accepts_reply_stop(self, response_event_id: str, room_id: str) -> bool:
        """Return whether a Stop on this event reaches a reply record in the room."""
        return await self.deps.delivery_gateway.accepts_reply_stop(response_event_id, room_id)

    async def finalize(
        self,
        response_event_id: str,
        stop_receipt_order: int,
        *,
        room_id: str,
    ) -> bool:
        """Record one user-stop intent on the reply it reaches, independently of runtime recovery order.

        Returns False, before writing anything, when the event's owning turn has
        no conversation target: a visible voice echo is such an owner, and
        there is no response to stop.

        The reply's records own its Stop: the span's exit, the approval's
        failure settlement, or the owed cancel note shows it. Nothing waits for
        the conversation. An event no reply owns has nothing to stop.
        """
        owner = self.deps.turn_store.turn_record_for_response_event_id(response_event_id)
        if owner is not None and owner.conversation_target is None:
            return False
        # A reaction on someone else's message, or one whose create resolved elsewhere, reaches no reply.
        return await self.deps.delivery_gateway.stop_reply(
            response_event_id,
            stop_receipt_order,
            room_id=room_id,
            # A turn that names the event already knows it; only a reply still
            # creating its event can be the target of a Stop nothing names yet.
            may_wait=owner is None,
        )
