"""Matrix transport adapter for journal-owned tool approvals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import nio

from mindroom import approval_manager
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import DeliveryStage
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    can_send_to_encrypted_room,
    resolve_room_encryption_for_delivery,
    send_room_event_result,
)
from mindroom.matrix.large_messages import content_fits_normal_event, sidecar_upload_is_usable, upload_json_sidecar
from mindroom.matrix.message_builder import build_matrix_edit_content, build_message_content, build_thread_relation
from mindroom.matrix.room_history_reads import find_outbox_delivery_event_id_via_room_messages
from mindroom.matrix_delivery import MatrixDeliveryWorker
from mindroom.tool_approval import DEFAULT_ROUTER_MANAGED_ROOM_REASON, ToolApprovalTransportError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.event_journal import (
        ApprovalContinuation,
        ApprovalDeliveryView,
        MatrixDelivery,
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

    @property
    def approval_store(self) -> ApprovalDeliveryView: ...

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
    ) -> str | None: ...

    def retry_approval_sources(self, room_id: str, source_event_ids: tuple[str, ...]) -> None: ...


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
    room_encrypted = await resolve_room_encryption_for_delivery(
        client,
        room_id,
        operation="offload_approval_full_arguments",
    )
    if room_encrypted is None:
        offloaded["approvable"] = False
        return offloaded
    mxc_uri, file_info = await upload_json_sidecar(
        client,
        room_id,
        full_arguments,
        room_encrypted=room_encrypted,
    )
    if not sidecar_upload_is_usable(mxc_uri, file_info, room_encrypted=room_encrypted):
        logger.warning(
            "approval_full_arguments_sidecar_unavailable",
            room_id=room_id,
            has_mxc_uri=bool(mxc_uri),
            has_file_info=bool(file_info),
        )
        offloaded["approvable"] = False
        return offloaded
    if room_encrypted:
        offloaded["full_arguments_file"] = file_info
    else:
        offloaded["full_arguments_url"] = mxc_uri
        offloaded["full_arguments_info"] = file_info
    return offloaded


@dataclass
class ApprovalMatrixTransport:
    """Prepare, send, and adopt journal-owned approval deliveries."""

    bot_provider: Callable[[str], _ApprovalTransportBot | None]

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

    async def deliver_unavailable_notice(
        self,
        continuation: ApprovalContinuation,
        reason: str,
    ) -> ApprovalDeliveryView | None:
        """Durably send or adopt one router-owned unavailable-owner notice."""
        bot = self.transport_bot(continuation.room_id)
        if bot is None or bot.client is None:
            return None
        client = bot.client
        if not can_send_to_encrypted_room(client, continuation.room_id, operation="send_approval_notice"):
            return None
        store = bot.approval_store
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

        delivery_id = await store.enqueue_unavailable_approval_notice(
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
                sending_device_id=self.transport_device_id(),
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
    ) -> dict[str, Any] | None:
        """Freeze relation and sidecar content before durable reservation."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None or not bot.running or bot.client is None:
            return None
        if not self._bot_has_approval_room(bot, room_id):
            raise ToolApprovalTransportError(DEFAULT_ROUTER_MANAGED_ROOM_REASON)
        if not can_send_to_encrypted_room(bot.client, room_id, operation="send_approval_event"):
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
        """Send one already-frozen approval event or deterministic edit."""
        bot = self.transport_bot(claimed.room_id)
        if bot is None or bot.client is None:
            raise ToolApprovalTransportError(DEFAULT_ROUTER_MANAGED_ROOM_REASON)
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
        """Adopt the exact card or terminal edit found after a device change."""
        bot = self.transport_bot(claimed.room_id)
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
        """Return the generic delivery ID carried by one exact visible card."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None:
            msg = f"Router approval transport cannot read {room_id} to verify a card action"
            raise approval_manager.UnverifiableApprovalCardError(msg)
        if not bot.running or bot.client is None:
            msg = f"Router approval transport is not ready to verify a card action in {room_id}"
            raise ToolApprovalTransportError(msg)
        if not self._bot_has_approval_room(bot, room_id):
            # Router-free agent rooms cannot contain cards from this transport.
            # Abstain so ordinary replies continue through normal text ingress.
            return None
        response = await bot.client.room_get_event(room_id, card_event_id)
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
        sender = self.transport_sender_id()
        source = event.source if isinstance(event.source, dict) else None
        if (
            sender is None
            or event.event_id != card_event_id
            or event.sender != sender
            or source is None
            or source.get("room_id") not in {None, room_id}
            or source.get("type") != "io.mindroom.tool_approval"
        ):
            return None
        content = source.get("content")
        if not isinstance(content, dict):
            return None
        approval_id = content.get("approval_id")
        return approval_id if isinstance(approval_id, str) and approval_id else None

    def _bot_has_approval_room(self, bot: _ApprovalTransportBot, room_id: str) -> bool:
        """Return whether one bot can safely post into an approval room."""
        return bot.client is not None and room_id in bot.approval_room_ids

    def transport_bot(self, room_id: str) -> _ApprovalTransportBot | None:
        """Return the live router bot serving one approval room."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None or not bot.running or bot.client is None:
            return None
        return bot if self._bot_has_approval_room(bot, room_id) else None

    def transport_sender_id(self) -> str | None:
        """Return the Matrix user id that owns approval cards."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None or bot.client is None:
            return None
        user_id = bot.client.user_id
        return user_id if isinstance(user_id, str) and user_id else None

    def transport_device_id(self) -> str | None:
        """Return the Matrix device that sends approval cards."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None or bot.client is None:
            return None
        device_id = bot.client.device_id
        return device_id if isinstance(device_id, str) and device_id else None

    async def send_notice(
        self,
        *,
        room_id: str,
        approval_event_id: str,
        thread_id: str | None,
        reason: str,
        transaction_id: str | None = None,
    ) -> bool:
        """Send one approval notice through router transport."""
        bot = self.transport_bot(room_id)
        if bot is None or bot.client is None:
            return False
        if not can_send_to_encrypted_room(bot.client, room_id, operation="send_approval_notice"):
            return False
        content = build_message_content(
            reason,
            thread_event_id=thread_id,
            reply_to_event_id=approval_event_id,
            extra_content={"msgtype": "m.notice"},
        )
        response = await send_room_event_result(
            bot.client,
            room_id,
            "m.room.message",
            content,
            transaction_id=transaction_id,
            operation="send_approval_notice",
        )
        return isinstance(response, nio.RoomSendResponse)
