"""Durable ordering and visible settlement for user stop reactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.delivery_gateway import DeliveryGateway, ReplyStop
    from mindroom.handled_turns import TurnRecord
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class UserStopReconcilerDeps:
    """Collaborators for durable stop settlement."""

    turn_store: TurnStore
    response_runner: ResponseRunner
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
        delivery_settled: bool = False,
        deleted_turn_id: str | None = None,
        reply_stop: ReplyStop | None = None,
    ) -> TurnRecord:
        stopped = await self.deps.turn_store.record_user_stopped_response(
            response_event_id,
            stop_receipt_order,
            delivery_settled=delivery_settled,
            deleted_turn_id=deleted_turn_id,
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

    def _should_cancel(self, source_event_id: str, stop_receipt_order: int) -> bool:
        current = self.deps.turn_store.get_turn_record(source_event_id)
        return current is None or (
            (current.latest_edit_receipt_order or 0) <= stop_receipt_order
            and not self._is_settled(current, stop_receipt_order)
        )

    async def _finalize_under_lock(
        self,
        response_event_id: str,
        stop_receipt_order: int,
        target: MessageTarget,
        on_current_stop_finalized: Callable[[], Awaitable[None]],
        approval_settled: bool,
    ) -> bool:
        async with self.deps.delivery_gateway.user_stop_scope(response_event_id) as deleted_turn_id:
            stopped = await self._record(response_event_id, stop_receipt_order, deleted_turn_id=deleted_turn_id)
            newer_edit_exists = (stopped.latest_edit_receipt_order or 0) > stop_receipt_order
            if not self._is_settled(stopped, stop_receipt_order):
                if (
                    not newer_edit_exists
                    and not approval_settled
                    and deleted_turn_id is None
                    and not await self.deps.delivery_gateway.finalize_user_stopped_response(
                        target,
                        response_event_id,
                    )
                ):
                    msg = f"Failed to finalize user-stopped response {response_event_id!r}"
                    raise RuntimeError(msg)
                stopped = await self._record(
                    response_event_id,
                    stop_receipt_order,
                    delivery_settled=True,
                    deleted_turn_id=deleted_turn_id,
                )
        if not self._is_settled(stopped, stop_receipt_order):
            return False
        if not newer_edit_exists:
            await on_current_stop_finalized()
        return True

    async def finalize(
        self,
        response_event_id: str,
        stop_receipt_order: int,
        on_current_stop_finalized: Callable[[], Awaitable[None]],
    ) -> bool:
        """Make one user-stop intent terminal independently of runtime recovery order.

        Returns False, before writing anything, when the event's owning turn has
        no conversation target: a visible voice echo is such an owner, and
        there is no response to stop.
        """
        owner = self.deps.turn_store.turn_record_for_response_event_id(response_event_id)
        if owner is not None and owner.conversation_target is None:
            return False
        # The reply's records learn the Stop in the transaction that records it on
        # the turn, so no terminal row can slip between the two, and before its
        # span is cancelled, so the span's exit renders the cancellation.
        reply_stop = self.deps.delivery_gateway.reply_stop(response_event_id, stop_receipt_order)
        async with self.deps.delivery_gateway.user_stop_scope(response_event_id) as deleted_turn_id:
            stopped_turn = await self._record(
                response_event_id,
                stop_receipt_order,
                deleted_turn_id=deleted_turn_id,
                reply_stop=reply_stop,
            )
        reply_owned = await self.deps.delivery_gateway.finish_reply_stop(reply_stop)
        target = stopped_turn.conversation_target
        if target is None:
            msg = f"User-stopped response {response_event_id!r} has no durable conversation target"
            raise RuntimeError(msg)
        source_event_id = stopped_turn.indexed_event_ids[0]
        stopped = await self.deps.response_runner.finalize_user_stop(
            response_event_id,
            source_event_id,
            target,
            stop_receipt_order,
            lambda: self._should_cancel(source_event_id, stop_receipt_order),
            lambda approval_settled: self._finalize_under_lock(
                response_event_id,
                stop_receipt_order,
                target,
                on_current_stop_finalized,
                approval_settled or reply_owned,
            ),
        )
        if not stopped:
            msg = f"User-stopped response {response_event_id!r} did not become durable"
            raise RuntimeError(msg)
        return True
