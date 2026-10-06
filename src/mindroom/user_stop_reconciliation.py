"""Durable ordering and visible settlement for user stop reactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.delivery_gateway import DeliveryGateway, ReplyStop
    from mindroom.handled_turns import TurnRecord
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class UserStopReconcilerDeps:
    """Collaborators for durable stop settlement."""

    turn_store: TurnStore
    delivery_gateway: DeliveryGateway


@dataclass
class UserStopReconciler:
    """Make one stop intent terminal in durable receipt order."""

    deps: UserStopReconcilerDeps

    @staticmethod
    def _is_settled(turn_record: TurnRecord, stop_receipt_order: int) -> bool:
        settled_order = turn_record.user_stop_settled_receipt_order
        return settled_order is not None and settled_order >= stop_receipt_order

    async def _record(
        self,
        response_event_id: str,
        stop_receipt_order: int,
        *,
        reply_stop: ReplyStop | None = None,
    ) -> TurnRecord:
        stopped = await self.deps.turn_store.record_user_stopped_response(
            response_event_id,
            stop_receipt_order,
            delivery_settled=True,
            turn_id=None if reply_stop is None else reply_stop.turn_id,
            also=reply_stop,
        )
        if (
            stopped is None
            or not stopped.completed
            or stopped.user_stop_receipt_order is None
            or stopped.user_stop_receipt_order < stop_receipt_order
        ):
            msg = f"User-stopped response {response_event_id!r} did not become durable"
            raise RuntimeError(msg)
        return stopped

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
        """Make one user-stop intent terminal independently of runtime recovery order.

        Returns False, before writing anything, when the event's owning turn has
        no conversation target: a visible voice echo is such an owner, and
        there is no response to stop.

        A reply's records own its Stop: the Stop commits on the reply in the
        transaction that records it on the turn, so no terminal row slips
        between them, and the span's exit, the approval's failure settlement,
        or the owed cancel note shows it. Nothing waits for the conversation.
        An event no reply owns has nothing to stop.
        """
        owner = self.deps.turn_store.turn_record_for_response_event_id(response_event_id)
        if owner is not None and owner.conversation_target is None:
            return False
        reply_stop = await self.deps.delivery_gateway.reply_stop(
            response_event_id,
            stop_receipt_order,
            room_id=room_id,
            # A turn that names the event already knows it; only a reply still
            # creating its event can be the target of a Stop nothing names yet.
            may_wait=owner is None,
        )
        if reply_stop.pending:
            # Recorded for the event's create, whose acknowledgement applies it.
            return True
        if not reply_stop.owned:
            # A reaction on someone else's message, or one whose create resolved elsewhere.
            return False
        stopped_turn = await self._record(response_event_id, stop_receipt_order, reply_stop=reply_stop)
        await self.deps.delivery_gateway.finish_reply_stop(reply_stop)
        if not self._is_settled(stopped_turn, stop_receipt_order):
            # The reply's create bound its event after the Stop looked.
            await self._record(response_event_id, stop_receipt_order)
        return True
