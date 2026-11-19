"""Matrix transport adapter for journal-owned tool approvals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import nio

from mindroom.approval import manager as approval_manager
from mindroom.approval.tool_approval import ToolApprovalTransportError
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import DeliveryStage
from mindroom.event_journal.matrix_delivery import MatrixDeliveryWorker
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    can_send_to_encrypted_room,
    send_room_event_result,
)
from mindroom.matrix.large_messages import content_fits_normal_event, sidecar_upload_is_usable, upload_json_sidecar
from mindroom.matrix.message_builder import build_matrix_edit_content, build_message_content, build_thread_relation
from mindroom.matrix.room_history_reads import find_outbox_delivery_event_id_via_room_messages

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import (
        ApprovalContinuation,
        ApprovalDeliveryView,
        MatrixDelivery,
        PrincipalStore,
    )
logger = get_logger(__name__)

_UNAVAILABLE_NOTICE_APPROVAL_ID_KEY = "io.mindroom.approval_unavailable_id"


def _approval_delivery_content(claimed: MatrixDelivery) -> dict[str, object]:
    """Return the exact physical payload used to send or reconcile a delivery."""
    content = dict(claimed.payload)
    if claimed.edits_event_id is None:
        return content
    return build_matrix_edit_content(claimed.edits_event_id, content)


class _ApprovalTransportBot(Protocol):
    """The live bot surface needed for card transport and source wakeups."""

    agent_name: str
    running: bool
    client: nio.AsyncClient | None

    @property
    def approval_room_ids(self) -> frozenset[str]: ...

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
    ) -> str | None: ...

    def retry_approval_sources(self, room_id: str, source_event_ids: tuple[str, ...]) -> None: ...

    def journal_principal(self) -> PrincipalStore: ...


async def _offload_oversized_full_arguments(
    client: nio.AsyncClient,
    room_id: str,
    send_content: dict[str, Any],
) -> dict[str, Any]:
    """Keep large evidence outside timed receipts, which gain decision metadata later."""
    full_arguments = send_content.get("full_arguments")
    if not isinstance(full_arguments, dict) or (
        not send_content.get("auto_approve_options") and content_fits_normal_event(send_content)
    ):
        return send_content
    offloaded = {key: value for key, value in send_content.items() if key != "full_arguments"}
    # The card is frozen here and sent later, perhaps after a retry or a
    # restart, by which time the room may have turned encrypted. So the
    # arguments are always encrypted, as every deferred delivery's sidecars are;
    # clients read an encrypted file in any room.
    mxc_uri, file_info = await upload_json_sidecar(client, room_id, full_arguments, room_encrypted=True, durable=True)
    if not sidecar_upload_is_usable(mxc_uri, file_info, room_encrypted=True):
        logger.warning(
            "approval_full_arguments_sidecar_unavailable",
            room_id=room_id,
            has_mxc_uri=bool(mxc_uri),
            has_file_info=bool(file_info),
        )
        offloaded["approvable"] = False
        return offloaded
    offloaded["full_arguments_file"] = file_info
    return offloaded


@dataclass
class ApprovalMatrixTransport:
    """Prepare, send, and adopt journal-owned approval deliveries."""

    bot_provider: Callable[[str], _ApprovalTransportBot | None]
    # The bot for a room's posts that have no answering agent; it also reads a clicked card event.
    room_poster: Callable[[str], _ApprovalTransportBot | None]
    # The store whose card rows name each card's sender.
    cards: Callable[[], ApprovalDeliveryView | None]

    async def wake_continuation_sources(
        self,
        entity_name: str,
        room_id: str,
        source_event_ids: tuple[str, ...],
    ) -> None:
        """Wake the exact owner after an atomic card decision makes work ready."""
        bot = self.bot_provider(entity_name)
        if bot is not None and bot.running:
            bot.retry_approval_sources(room_id, source_event_ids)

    def responder_principal_id(self, entity_name: str) -> str | None:
        """Name the journal principal of one entity's Matrix account, as its paused replies record it."""
        bot = self.bot_provider(entity_name)
        return None if bot is None else bot.journal_principal().principal_id

    async def deliver_unavailable_notice(
        self,
        continuation: ApprovalContinuation,
        reason: str,
    ) -> ApprovalDeliveryView | None:
        """Durably send or adopt one unavailable-owner notice through the room's poster."""
        bot = self.room_poster(continuation.room_id)
        if bot is None or bot.client is None:
            return None
        client = bot.client
        if not can_send_to_encrypted_room(client, continuation.room_id, operation="send_approval_notice"):
            return None
        store = bot.journal_principal()
        content = build_message_content(
            reason,
            thread_event_id=continuation.thread_id,
            reply_to_event_id=continuation.response_event_id,
            extra_content={
                "msgtype": "m.notice",
                _UNAVAILABLE_NOTICE_APPROVAL_ID_KEY: continuation.approval_id,
            },
        )

        async def send(claimed: MatrixDelivery) -> str:
            response = await send_room_event_result(
                client,
                claimed.room_id,
                "m.room.message",
                dict(claimed.payload),
                transaction_id=claimed.transaction_id,
                operation="send_approval_notice",
            )
            if not isinstance(response, nio.RoomSendResponse):
                msg = f"Matrix refused unavailable-owner notice for {continuation.approval_id!r}: {response}"
                raise ToolApprovalTransportError(msg)
            return str(response.event_id)

        async def resolve_delivered(claimed: MatrixDelivery) -> str | None:
            response_sender = client.user_id
            if not response_sender:
                return None
            return await find_outbox_delivery_event_id_via_room_messages(
                client,
                claimed.room_id,
                delivery_sender=response_sender,
                source_event_ids=(continuation.response_event_id,),
                delivery_content=claimed.payload,
                delivery_event_type=claimed.event_type,
            )

        delivery_id = await store.approvals.enqueue_unavailable_approval_notice(
            approval_id=continuation.approval_id,
            room_id=continuation.room_id,
            thread_id=continuation.thread_id,
            payload=content,
        )
        if delivery_id is None:
            return None
        try:
            delivered = await MatrixDeliveryWorker(
                store=store,
                send=send,
                event_type="m.room.message",
                sending_device_id=client.device_id or None,
                resolve_delivered=resolve_delivered,
            ).flush(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
        except ToolApprovalTransportError:
            logger.warning(
                "approval_unavailable_notice_send_failed",
                approval_id=continuation.approval_id,
                room_id=continuation.room_id,
                exc_info=True,
            )
            return None
        return store if delivered is not None else None

    async def _approval_thread_relation(
        self,
        room_id: str,
        thread_id: str,
        agent_name: str,
    ) -> dict[str, object]:
        """Return a threaded relation payload for approval events."""
        bot = self.bot_provider(agent_name)
        latest_thread_event_id = thread_id
        if bot is not None:
            resolved = await bot.latest_thread_event_id_if_needed(room_id, thread_id)
            if resolved is not None:
                latest_thread_event_id = resolved
        return build_thread_relation(
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
        )

    async def prepare_approval_event(
        self,
        room_id: str,
        thread_id: str | None,
        content: dict[str, Any],
        sender_entity: str,
    ) -> dict[str, Any] | None:
        """Freeze relation and sidecar content for the entity that will send the card, before durable reservation."""
        bot = self.sender_bot(sender_entity, room_id)
        if (
            bot is None
            or bot.client is None
            or not can_send_to_encrypted_room(bot.client, room_id, operation="send_approval_event")
        ):
            return None
        send_content = dict(content)
        if thread_id is not None:
            agent_name = send_content.get("agent_name")
            send_content["m.relates_to"] = await self._approval_thread_relation(
                room_id,
                thread_id,
                agent_name if isinstance(agent_name, str) and agent_name else bot.agent_name,
            )
        return await _offload_oversized_full_arguments(bot.client, room_id, send_content)

    async def send_approval_delivery(self, claimed: MatrixDelivery) -> str:
        """Send one already-frozen approval event or deterministic edit through the account of the card's sender."""
        bot = self.sender_bot(await self._card_sender(claimed.delivery_id), claimed.room_id)
        if bot is None or bot.client is None:
            msg = f"The sender of approval delivery {claimed.delivery_id!r} is not running in {claimed.room_id}"
            raise ToolApprovalTransportError(msg)
        response = await send_room_event_result(
            bot.client,
            claimed.room_id,
            claimed.event_type,
            _approval_delivery_content(claimed),
            transaction_id=claimed.transaction_id,
            operation="send_approval_delivery",
        )
        if not isinstance(response, nio.RoomSendResponse):
            msg = f"Matrix refused approval delivery {claimed.delivery_id!r}: {response}"
            raise ToolApprovalTransportError(msg)
        return str(response.event_id)

    async def resolve_approval_delivery(self, claimed: MatrixDelivery) -> str | None:
        """Adopt the exact card or terminal edit its sender left in the room before a device change."""
        bot = self.sender_bot(await self._card_sender(claimed.delivery_id), claimed.room_id)
        if bot is None or bot.client is None:
            return None
        sender = bot.client.user_id
        if not isinstance(sender, str) or not sender:
            return None
        return await find_outbox_delivery_event_id_via_room_messages(
            bot.client,
            claimed.room_id,
            delivery_sender=sender,
            source_event_ids=(),
            delivery_content=_approval_delivery_content(claimed),
            delivery_event_type=claimed.event_type,
        )

    async def resolve_approval_action_delivery(self, room_id: str, card_event_id: str) -> str | None:
        """Return the generic delivery ID carried by one exact visible card that its recorded sender sent."""
        reader = self.room_poster(room_id)
        if reader is None or reader.client is None:
            # No running bot can read the room, so nothing here says the event is a card.
            # Abstain so ordinary replies continue through normal text ingress.
            return None
        response = await reader.client.room_get_event(room_id, card_event_id)
        if isinstance(response, nio.RoomGetEventError) and response.status_code in {
            "M_FORBIDDEN",
            "M_NOT_FOUND",
        }:
            msg = f"Matrix cannot verify approval card {card_event_id!r}: {response}"
            raise approval_manager.UnverifiableApprovalCardError(msg)
        if not isinstance(response, nio.RoomGetEventResponse):
            msg = f"Matrix could not verify approval card {card_event_id!r}: {response}"
            raise ToolApprovalTransportError(msg)
        event = response.event
        if isinstance(event, nio.MegolmEvent):
            msg = f"Matrix could not decrypt approval card {card_event_id!r}"
            raise ToolApprovalTransportError(msg)
        source = event.source if isinstance(event.source, dict) else None
        if (
            event.event_id != card_event_id
            or source is None
            or source.get("room_id") not in {None, room_id}
            or source.get("type") != "io.mindroom.tool_approval"
        ):
            return None
        content = source.get("content")
        approval_id = content.get("approval_id") if isinstance(content, dict) else None
        if not isinstance(approval_id, str) or not approval_id:
            return None
        sender = self.sender_bot(await self._card_sender(approval_id), room_id)
        account = None if sender is None or sender.client is None else sender.client.user_id
        return approval_id if account == event.sender else None

    def sender_bot(self, entity_name: str | None, room_id: str) -> _ApprovalTransportBot | None:
        """Return the running bot that sends and edits one entity's cards in a room it is in."""
        bot = self._entity_bot(entity_name)
        if bot is None or not bot.running or bot.client is None or room_id not in bot.approval_room_ids:
            return None
        return bot

    def _entity_bot(self, entity_name: str | None) -> _ApprovalTransportBot | None:
        """Return the bot of the entity a card row names as its sender."""
        # A card without a sender is one the router sent; see ``event_journal/legacy_approval_senders.py``.
        return self.bot_provider(entity_name or ROUTER_AGENT_NAME)

    async def _card_sender(self, delivery_id: str) -> str | None:
        """Return the entity a card row records as its sender."""
        cards = self.cards()
        return None if cards is None else await cards.approvals.approval_card_sender(delivery_id=delivery_id)

    async def sender_device_id(self, delivery_id: str) -> str | None:
        """Return the Matrix device of the account that sends one card."""
        bot = self._entity_bot(await self._card_sender(delivery_id))
        return None if bot is None or bot.client is None else bot.client.device_id or None

    async def send_notice(
        self,
        *,
        room_id: str,
        approval_event_id: str,
        thread_id: str | None,
        reason: str,
    ) -> bool:
        """Send one approval notice through the room's poster."""
        bot = self.room_poster(room_id)
        if bot is None or bot.client is None:
            return False
        content = build_message_content(
            reason,
            thread_event_id=thread_id,
            reply_to_event_id=approval_event_id,
            extra_content={"msgtype": "m.notice"},
        )
        # send_room_event_result refuses an encrypted room it cannot protect.
        response = await send_room_event_result(
            bot.client,
            room_id,
            "m.room.message",
            content,
            operation="send_approval_notice",
        )
        return isinstance(response, nio.RoomSendResponse)
