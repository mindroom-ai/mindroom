"""Durable adoption and delivery of non-model Matrix responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY, VISIBLE_ROUTER_VOICE_ECHO_KEY
from mindroom.delivery_gateway import SendTextRequest
from mindroom.event_journal import DeliveryStage
from mindroom.matrix.room_history_reads import find_response_event_ids_via_room_messages
from mindroom.turn_record import canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping

    import nio
    import structlog

    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.handled_turns import TurnRecord
    from mindroom.message_target import MessageTarget
    from mindroom.runtime_protocols import SupportsClientConfig
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class VisibleResponseReconcilerDeps:
    """Collaborators for durable non-model response delivery."""

    runtime: SupportsClientConfig
    logger: structlog.stdlib.BoundLogger
    response_sender: str
    turn_store: TurnStore
    delivery_gateway: DeliveryGateway
    settle_ignored_sources: Callable[[tuple[str, ...]], Awaitable[None]]


@dataclass
class VisibleResponseReconciler:
    """Own visible-event adoption around durable pending turns."""

    deps: VisibleResponseReconcilerDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for visible response reconciliation"
            raise RuntimeError(msg)
        return client

    async def recovered_response_event_id(
        self,
        handled_turn: TurnRecord,
        *,
        room_id: str,
        excluded_event_ids: Collection[str] = (),
    ) -> str | None:
        """Return the durable visible response owned by a replayed turn."""
        incomplete_records = tuple(
            record
            for source_event_id in handled_turn.source_event_ids
            if (record := self.deps.turn_store.get_turn_record(source_event_id)) is not None and not record.completed
        )
        response_event_ids = {
            record.response_event_id for record in incomplete_records if record.response_event_id is not None
        }
        response_event_ids.difference_update(excluded_event_ids)
        if len(response_event_ids) > 1:
            msg = "Recovered coalesced turn has conflicting visible response event IDs"
            raise RuntimeError(msg)

        def response_source_is_canonical(source: Mapping[str, Any]) -> bool:
            content = source.get("content")
            return not isinstance(content, dict) or content.get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is not True

        def response_source_is_terminal(source: Mapping[str, Any]) -> bool:
            content = source.get("content")
            return (
                response_source_is_canonical(source)
                and isinstance(content, dict)
                and content.get(STREAM_STATUS_KEY) == STREAM_STATUS_COMPLETED
            )

        if response_event_ids:
            terminal_response_event_ids = set(
                await find_response_event_ids_via_room_messages(
                    self._client(),
                    room_id,
                    response_sender=self.deps.response_sender,
                    source_event_ids=handled_turn.source_event_ids,
                    response_source_filter=response_source_is_terminal,
                ),
            )
            terminal_response_event_ids.difference_update(excluded_event_ids)
            if len(terminal_response_event_ids) > 1:
                msg = "Recovered turn has multiple terminal visible Matrix responses"
                raise RuntimeError(msg)
            if terminal_response_event_ids:
                recovered_response_event_id = next(iter(terminal_response_event_ids))
            else:
                recovered_response_event_id = next(iter(response_event_ids))
        else:
            response_event_ids = set(
                await find_response_event_ids_via_room_messages(
                    self._client(),
                    room_id,
                    response_sender=self.deps.response_sender,
                    source_event_ids=handled_turn.source_event_ids,
                    response_source_filter=response_source_is_canonical,
                ),
            )
            response_event_ids.difference_update(excluded_event_ids)
            self.deps.logger.info(
                "dispatch_recovery_response_lookup",
                room_id=room_id,
                source_event_ids=handled_turn.source_event_ids,
                response_event_ids=sorted(response_event_ids),
            )
            if len(response_event_ids) > 1:
                msg = "Recovered turn has multiple visible Matrix responses"
                raise RuntimeError(msg)
            recovered_response_event_id = next(iter(response_event_ids), None)
        if recovered_response_event_id is not None:
            await self.record_pending_visible_response(handled_turn, recovered_response_event_id)
        return recovered_response_event_id

    async def settle_source_events_ignored(self, handled_turn: TurnRecord) -> None:
        """Compact exact callback obligations without growing the handled-turn ledger."""
        await self.deps.settle_ignored_sources(handled_turn.source_event_ids)

    async def record_pending_visible_response(self, handled_turn: TurnRecord, response_event_id: str) -> None:
        """Durably bind one visible response to its incomplete turn before generation."""
        await self.deps.turn_store.record_pending_turn(
            canonicalize_turn_record(handled_turn, response_event_id=response_event_id, completed=False),
        )

    async def deliver_recoverable_text(
        self,
        handled_turn: TurnRecord,
        *,
        target: MessageTarget,
        response_text: str,
        recovered_response_event_id: str | None,
        skip_mentions: bool = False,
        as_placeholder: bool = False,
    ) -> str | None:
        """Send and durably bind one non-model reply unless recovery already found it.

        Every reply here has a turn behind it, so every one goes through the
        same claim-before-send row a model answer uses. Two things follow from
        that, and both are the point.

        The journal sources this turn answers settle inside the enqueue, so the
        answer becoming durably owed and the turn stopping being the journal's
        work are one commit. On the direct path the terminal record would land
        in the handled-turn ledger first and the journal would settle
        afterwards, leaving a window where a pending row describes finished
        work -- the window the degraded replay guard has to consult two records
        to survive.

        And a crashed send is recovered by resending the frozen row rather than
        by scanning the room for what might already be there. The scan still
        runs ahead of the send here, because a row from a previous membership
        is gone and its answer is not, but it stops being the only thing
        standing between a crash and a lost reply.

        Only callers that send exactly once per ``(turn, stage)`` may use this.
        The outbox freezes a row at its first attempt, so a second send under
        the same pair would be refused and its text would never reach the room.
        A caller that sends a placeholder and then an answer has two stages
        available and should use them.

        ``as_placeholder`` marks a send that a later answer edits rather than
        replaces, and it is the caller's own word for what the message is --
        the delivery stage it maps to is the outbox's business, not theirs.
        Only an answer settles the journal sources, because only an answer
        discharges a turn; a placeholder that settled would leave a crash
        before the model finished with nothing pending to replay and
        "Thinking..." in the room for good.

        A send that genuinely is not a turn -- a voice echo, a reconciliation
        notice -- has no identity a restart can resolve and does not belong
        here at all; it builds its own ``SendTextRequest`` and the gateway
        gives it the direct path.
        """
        if recovered_response_event_id is not None:
            return recovered_response_event_id
        response_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=target,
                response_text=response_text,
                skip_mentions=skip_mentions,
                delivery_turn_id=handled_turn.anchor_event_id,
                delivery_stage=DeliveryStage.INITIAL if as_placeholder else DeliveryStage.FINAL,
            ),
        )
        if response_event_id is not None:
            await self.record_pending_visible_response(handled_turn, response_event_id)
        return response_event_id

    async def prepare_visible_delivery_turn(
        self,
        handled_turn: TurnRecord,
        *,
        requester_id: str,
        correlation_id: str,
        target: MessageTarget,
        excluded_event_ids: Collection[str] = (),
    ) -> tuple[TurnRecord | None, str | None]:
        """Persist one non-model delivery intent and recover any visible event."""
        reconcile_visible_response = self.deps.turn_store.has_pending_response_intent(
            handled_turn.source_event_ids,
        )
        tracked_turn = self.deps.turn_store.attach_response_context(
            canonicalize_turn_record(
                handled_turn,
                requester_id=requester_id,
                correlation_id=correlation_id,
            ),
            history_scope=None,
            conversation_target=target,
        )
        pending_turn = await self.deps.turn_store.record_pending_turn(tracked_turn)
        if pending_turn is None or pending_turn.completed:
            return None, None
        if pending_turn.redacted_source_event_ids:
            await self.settle_source_events_ignored(pending_turn)
            return None, None
        recovered_response_event_id = (
            await self.recovered_response_event_id(
                pending_turn,
                room_id=target.room_id,
                excluded_event_ids=excluded_event_ids,
            )
            if reconcile_visible_response
            else None
        )
        return pending_turn, recovered_response_event_id
