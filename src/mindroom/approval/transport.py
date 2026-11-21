"""Matrix transport adapter for journal-owned tool approvals."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

import nio

from mindroom.approval import manager as approval_manager
from mindroom.approval.tool_approval import ToolApprovalTransportError
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import DeliveryStage
from mindroom.event_journal.identity import agent_of_principal, matrix_user_of_principal
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
    from collections.abc import Callable, Mapping

    from mindroom.event_journal import (
        ApprovalContinuation,
        ApprovalDeliveryView,
        MatrixDelivery,
        PrincipalStore,
    )
    from mindroom.event_journal.matrix_delivery import ResolveDelivered, SendDelivery
logger = get_logger(__name__)

_UNAVAILABLE_NOTICE_APPROVAL_ID_KEY = "io.mindroom.approval_unavailable_id"
_UNREADABLE = frozenset({"M_FORBIDDEN", "M_NOT_FOUND"})


def _approval_delivery_content(claimed: MatrixDelivery) -> dict[str, object]:
    """Return the exact physical payload used to send or reconcile a delivery."""
    content = dict(claimed.payload)
    if claimed.edits_event_id is None:
        return content
    return build_matrix_edit_content(claimed.edits_event_id, content)


class _ApprovalTransportBot(Protocol):
    """The live bot surface needed for card transport and source wakeups."""

    @property
    def agent_name(self) -> str: ...

    @property
    def running(self) -> bool: ...

    @property
    def client(self) -> nio.AsyncClient | None: ...

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

    bots: Callable[[], Mapping[str, _ApprovalTransportBot]]
    # The bot for a room's posts that have no answering agent.
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
        bot = self.bots().get(entity_name)
        if bot is not None and bot.running:
            bot.retry_approval_sources(room_id, source_event_ids)

    def responder_principal_id(self, entity_name: str) -> str | None:
        """Name the journal principal of one entity's Matrix account, as its paused replies record it."""
        bot = self.bots().get(entity_name)
        return None if bot is None else bot.journal_principal().principal_id

    async def deliver_unavailable_notice(
        self,
        continuation: ApprovalContinuation,
        reason: str,
    ) -> bool:
        """Durably send or adopt one unavailable-owner notice through the room's poster, or the account that began it.

        An acknowledged notice is delivered whether or not the account that posted it still runs.
        """
        cards = self.cards()
        first = None if cards is None else await cards.approvals.unavailable_notice(continuation.approval_id)
        if first is not None and first[2]:
            return True
        bot = self.room_poster(continuation.room_id) if first is None else self.bots().get(agent_of_principal(first[0]))
        if bot is None or bot.client is None:
            return False
        client = bot.client
        if not can_send_to_encrypted_room(client, continuation.room_id, operation="send_approval_notice"):
            return False
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

        delivery_id = (
            first[1]
            if first is not None
            else await store.approvals.enqueue_unavailable_approval_notice(
                approval_id=continuation.approval_id,
                room_id=continuation.room_id,
                thread_id=continuation.thread_id,
                payload=content,
            )
        )
        if delivery_id is None:
            return False
        try:
            delivered = await MatrixDeliveryWorker(
                store=store,
                send=send,
                event_type="m.room.message",
                sending_device_id=client.device_id or None,
                resolve_delivered=partial(
                    self._resolve_approval_delivery,
                    client,
                    source_event_ids=(continuation.response_event_id,),
                ),
            ).flush(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
        except ToolApprovalTransportError:
            logger.warning(
                "approval_unavailable_notice_send_failed",
                approval_id=continuation.approval_id,
                room_id=continuation.room_id,
                exc_info=True,
            )
            return False
        return delivered is not None

    async def _approval_thread_relation(
        self,
        room_id: str,
        thread_id: str,
        agent_name: str,
    ) -> dict[str, object]:
        """Return a threaded relation payload for approval events."""
        bot = self.bots().get(agent_name)
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
        bot = self._joined_bot(sender_entity, room_id)
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

    async def pinned_delivery(self, delivery_id: str | None) -> tuple[SendDelivery, ResolveDelivered, str | None]:
        """Pin the one client a card's deliveries go out through, with its device, before a delivery is claimed.

        A sender that is not ready, or has no bot while it is replaced, raises: nothing is claimed and a later pass retries.
        """
        client = None
        if delivery_id is not None:
            cards = self.cards()
            card = None if cards is None else await cards.approvals.approval_card_sender(delivery_id=delivery_id)
            bot = None if card is None else self.sender_bot(*card)
            if bot is None:
                msg = f"The sender of approval delivery {delivery_id!r} is not ready"
                raise ToolApprovalTransportError(msg)
            client = bot.client
        send = partial(self._send_approval_delivery, client)
        return (
            send,
            partial(self._resolve_approval_delivery, client),
            None if client is None else client.device_id or None,
        )

    async def _send_approval_delivery(self, client: nio.AsyncClient | None, claimed: MatrixDelivery) -> str:
        """Send one already-frozen approval event or deterministic edit through the pinned client of its sender."""
        if client is None:
            msg = f"Approval delivery {claimed.delivery_id!r} has no pinned sender"
            raise ToolApprovalTransportError(msg)
        response = await send_room_event_result(
            client,
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

    async def _resolve_approval_delivery(
        self,
        client: nio.AsyncClient | None,
        claimed: MatrixDelivery,
        *,
        source_event_ids: tuple[str, ...] = (),
    ) -> str | None:
        """Adopt the exact card, terminal edit, or notice its sender left before a device change."""
        if client is None or not client.user_id:
            return None
        return await find_outbox_delivery_event_id_via_room_messages(
            client,
            claimed.room_id,
            delivery_sender=client.user_id,
            source_event_ids=source_event_ids,
            delivery_content=_approval_delivery_content(claimed),
            delivery_event_type=claimed.event_type,
        )

    async def resolve_approval_action_delivery(self, room_id: str, card_event_id: str) -> str | None:
        """Return the generic delivery ID carried by one exact visible card that its recorded sender sent.

        Every running bot in the room may read the event: the card's sender can read what another account cannot.
        With no such bot nothing says the event is a card, so an ordinary reply goes on as text.
        """
        cards, refused, failed = self.cards(), None, None
        # The fleet changes while a reader waits on Matrix, so the readers are named before the first read.
        joined = [bot.client for bot in self.bots().values() if bot.running and bot.client is not None]
        for client in [client for client in joined if room_id in client.rooms]:
            response = await client.room_get_event(room_id, card_event_id)
            event = response.event if isinstance(response, nio.RoomGetEventResponse) else None
            source = None if event is None or isinstance(event, nio.MegolmEvent) else event.source
            if not isinstance(source, dict):
                final = isinstance(response, nio.RoomGetEventError) and response.status_code in _UNREADABLE
                refused, failed = (response, failed) if final else (refused, response)
                continue
            content = source.get("content")
            approval_id = content.get("approval_id") if isinstance(content, dict) else None
            if cards is None or event is None or not isinstance(approval_id, str):
                return None
            card = await cards.approvals.approval_card_sender(delivery_id=approval_id)
            if (
                card is None
                or event.event_id != card_event_id
                or source.get("room_id") not in {None, room_id}
                or source.get("type") != "io.mindroom.tool_approval"
                or event.sender != matrix_user_of_principal(card[0] or cards.principal_id)
            ):
                return None
            return approval_id
        if failed is not None:
            msg = f"Matrix could not verify approval card {card_event_id!r}: {failed}"
            raise ToolApprovalTransportError(msg)
        if refused is not None:
            msg = f"Matrix cannot verify approval card {card_event_id!r}: {refused}"
            raise approval_manager.UnverifiableApprovalCardError(msg)
        return None

    def sender_bot(self, sender_principal_id: str | None, room_id: str) -> _ApprovalTransportBot | None:
        """Return the running bot of a card's sender while its own client is joined to the card's room."""
        # LEGACY_COMPAT: Approval cards the router sent, stored without a sender; see legacy_approval_records.py.
        entity = agent_of_principal(sender_principal_id) if sender_principal_id else ROUTER_AGENT_NAME
        return self._joined_bot(entity, room_id)

    def _joined_bot(self, entity_name: str, room_id: str) -> _ApprovalTransportBot | None:
        bot = self.bots().get(entity_name)
        joined = bot is not None and bot.running and bot.client is not None and room_id in bot.client.rooms
        return bot if joined else None

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
