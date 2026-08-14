"""Matrix transport adapter for journal-owned tool approvals."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import DeliveryStage, unavailable_notice_turn_id
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    can_send_to_encrypted_room,
    resolve_room_encryption_for_delivery,
    send_room_event_result,
)
from mindroom.matrix.large_messages import content_fits_normal_event, sidecar_upload_is_usable, upload_json_sidecar
from mindroom.matrix.message_builder import build_matrix_edit_content, build_message_content, build_thread_relation
from mindroom.matrix.room_history_reads import (
    find_approval_card_event_id_via_room_messages,
    find_response_event_ids_via_room_messages,
)
from mindroom.matrix_delivery import MatrixDeliveryWorker
from mindroom.tool_approval import (
    DEFAULT_ROUTER_MANAGED_ROOM_REASON,
    ToolApprovalTransportError,
    expire_continuation_approval_cards,
    initialize_approval_runtime,
    recover_approval_cards_on_startup,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import (
        ApprovalContinuation,
        EventJournalStore,
        MatrixDelivery,
        PrincipalStore,
    )

logger = get_logger(__name__)

_STARTUP_CLEANUP_INITIAL_RETRY_SECONDS = 1.0
_STARTUP_CLEANUP_MAX_RETRY_SECONDS = 30.0
_STARTUP_CLEANUP_ATTEMPTS_BEFORE_ESCALATION = 10
_UNAVAILABLE_OWNER_SCAN_LIMIT = 100
_UNAVAILABLE_NOTICE_APPROVAL_ID_KEY = "io.mindroom.approval_unavailable_id"


class _ApprovalTransportBot(Protocol):
    """The live bot surface needed for card transport and source wakeups."""

    agent_name: str
    running: bool
    client: nio.AsyncClient | None

    @property
    def approval_room_ids(self) -> frozenset[str]: ...

    @property
    def approval_store(self) -> PrincipalStore: ...

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
    ) -> str | None: ...

    def retry_approval_sources(self, source_event_ids: tuple[str, ...]) -> None: ...


def _approval_relation_agent_name(content: dict[str, Any], *, fallback: str) -> str:
    agent_name = content.get("agent_name")
    return agent_name if isinstance(agent_name, str) and agent_name else fallback


async def _offload_oversized_full_arguments(
    client: nio.AsyncClient,
    room_id: str,
    send_content: dict[str, Any],
) -> dict[str, Any]:
    """Move oversized arguments to a sidecar without weakening approval integrity."""
    full_arguments = send_content.get("full_arguments")
    if not isinstance(full_arguments, dict) or content_fits_normal_event(send_content):
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
    """Own Matrix card transport and permanent-owner cleanup."""

    runtime_paths: RuntimePaths
    bot_provider: Callable[[str], _ApprovalTransportBot | None]
    cards_provider: Callable[[], PrincipalStore | None]
    journal_provider: Callable[[], EventJournalStore] | None = None
    entity_configured: Callable[[str], bool] | None = None
    entity_permanently_unavailable: Callable[[str], bool] | None = None
    recover_unavailable_final: Callable[[str, ApprovalContinuation], Awaitable[bool]] | None = None
    _startup_router_ready_for_cleanup: bool = field(default=False, init=False, repr=False)
    _startup_runtime_support_ready_for_cleanup: bool = field(default=False, init=False, repr=False)
    _startup_cleanup_done: bool = field(default=False, init=False, repr=False)
    _startup_cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _startup_cleanup_retry: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _startup_cleanup_retry_delay: float = field(
        default=_STARTUP_CLEANUP_INITIAL_RETRY_SECONDS,
        init=False,
        repr=False,
    )
    _startup_cleanup_attempts: int = field(default=0, init=False, repr=False)

    def bind_approval_runtime(self) -> None:
        """Bind approval manager hooks to the current Matrix transport."""
        initialize_approval_runtime(
            self.runtime_paths,
            prepare_event=self.prepare_approval_event,
            send_delivery=self.send_approval_delivery,
            resolve_delivery=self.resolve_approval_delivery,
            cards=self.cards_provider(),
            transport_sender=self.transport_sender_id,
            sending_device=self.transport_device_id,
            continuation_ready=self._wake_continuation_sources,
        )

    async def _wake_continuation_sources(
        self,
        entity_name: str,
        source_event_ids: tuple[str, ...],
    ) -> None:
        """Wake the exact owner after an atomic card decision makes work ready."""
        bot = self.bot_provider(entity_name)
        if bot is not None and bot.running:
            bot.retry_approval_sources(source_event_ids)

    def _unavailable_entity_reason(self, entity_name: str) -> str | None:
        permanently_unavailable = (
            self.entity_permanently_unavailable is not None and self.entity_permanently_unavailable(entity_name)
        )
        configured = self.entity_configured is None or self.entity_configured(entity_name)
        if configured and not permanently_unavailable:
            return None
        if permanently_unavailable:
            return f"Requesting agent '{entity_name}' could not start and is unavailable."
        return f"Requesting agent '{entity_name}' is no longer available."

    async def reconcile_unavailable_entities(self, entity_names: Iterable[str]) -> None:
        """Fail closed continuations whose owner cannot ever run them."""
        names = set(entity_names)
        if not names:
            return
        if not await self._reconcile_unavailable_owner_pages(names):
            self._startup_cleanup_done = False
            self._schedule_startup_cleanup_retry()

    async def _reconcile_unavailable_owner_pages(self, entity_names: set[str] | None) -> bool:
        """Settle unavailable owners across one complete cursor scan."""
        journal = None if self.journal_provider is None else self.journal_provider()
        if journal is None:
            return True
        complete = True
        cursor: tuple[str, str] | None = None
        while True:
            if entity_names is None:
                owners = await journal.approval_continuations(
                    limit=_UNAVAILABLE_OWNER_SCAN_LIMIT,
                    after=cursor,
                )
            else:
                owners = await journal.approval_continuations_for_entities(
                    entity_names,
                    limit=_UNAVAILABLE_OWNER_SCAN_LIMIT,
                    after=cursor,
                )
            if not owners:
                break
            cursor = (owners[-1][1].entity_name, owners[-1][1].approval_id)
            for principal_id, continuation in owners:
                reason = self._unavailable_entity_reason(continuation.entity_name)
                if reason is not None:
                    complete = await self._discard_unavailable(principal_id, continuation, reason) and complete
            if len(owners) < _UNAVAILABLE_OWNER_SCAN_LIMIT:
                break
        return complete

    @staticmethod
    def _is_unavailable_notice(event_source: Mapping[str, Any], *, approval_id: str) -> bool:
        """Return whether one room event is the exact durable fallback notice."""
        content = event_source.get("content")
        return isinstance(content, Mapping) and content.get(_UNAVAILABLE_NOTICE_APPROVAL_ID_KEY) == approval_id

    async def _deliver_unavailable_notice(
        self,
        continuation: ApprovalContinuation,
        reason: str,
    ) -> PrincipalStore | None:
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
            delivered = await find_response_event_ids_via_room_messages(
                client,
                claimed.room_id,
                response_sender=response_sender,
                source_event_ids=(continuation.response_event_id,),
                response_source_filter=lambda source: self._is_unavailable_notice(
                    source,
                    approval_id=continuation.approval_id,
                ),
            )
            if len(delivered) > 1:
                msg = (
                    f"Approval continuation {continuation.approval_id!r} has {len(delivered)} unavailable-owner notices"
                )
                raise RuntimeError(msg)
            return next(iter(delivered), None)

        try:
            delivered = await MatrixDeliveryWorker(
                store=store,
                send=send,
                event_type="m.room.message",
                sending_device_id=self.transport_device_id(),
                resolve_delivered=resolve_delivered,
            ).deliver(
                delivery_id=unavailable_notice_turn_id(continuation.approval_id),
                stage=DeliveryStage.FINAL,
                room_id=continuation.room_id,
                thread_id=continuation.thread_id,
                payload=content,
            )
        except ToolApprovalTransportError:
            logger.warning(
                "approval_unavailable_notice_send_failed",
                approval_id=continuation.approval_id,
                room_id=continuation.room_id,
                exc_info=True,
            )
            return None
        return store if delivered is not None else None

    async def _discard_unavailable(
        self,
        principal_id: str,
        continuation: ApprovalContinuation,
        reason: str,
    ) -> bool:
        """Expire visible cards, then atomically release the removed owner's sources."""
        assert self.journal_provider is not None
        store = self.journal_provider().principal(principal_id)
        current = await store.approval_continuation(continuation.approval_id)
        if current is None:
            return True
        final_delivery = await store.load_matrix_delivery(
            delivery_id=current.source_event_ids[0],
            stage=DeliveryStage.FINAL,
        )
        if final_delivery is not None:
            return self.recover_unavailable_final is not None and await self.recover_unavailable_final(
                principal_id,
                current,
            )
        if current.state != "failing":
            current = await store.request_approval_failure(
                current.approval_id,
                reason,
                expected_state=current.state,
                expected_generation=current.generation,
                expected_runtime_generation=current.runtime_generation,
            )
            if current is None:
                return False
        if not await expire_continuation_approval_cards(current.approval_id):
            return False
        notice_store = await self._deliver_unavailable_notice(current, reason)
        if notice_store is None:
            return False
        return await store.discard_unavailable_approval_continuation(
            current.approval_id,
            notice_principal_id=notice_store.principal_id,
        )

    async def _reconcile_startup_unavailable(self) -> bool:
        """Clean rows left by entities removed while the process was offline."""
        return await self._reconcile_unavailable_owner_pages(None)

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
            send_content["m.relates_to"] = await self._approval_thread_relation(
                room_id,
                thread_id,
                _approval_relation_agent_name(send_content, fallback=bot.agent_name),
            )
        return await _offload_oversized_full_arguments(bot.client, room_id, send_content)

    async def send_approval_delivery(self, claimed: MatrixDelivery) -> str:
        """Send one already-frozen approval event or deterministic edit."""
        bot = self.transport_bot(claimed.room_id)
        if bot is None or bot.client is None:
            raise ToolApprovalTransportError(DEFAULT_ROUTER_MANAGED_ROOM_REASON)
        content = dict(claimed.payload)
        if claimed.edits_event_id is not None:
            content.pop("thread_id", None)
            content = build_matrix_edit_content(claimed.edits_event_id, content)
        response = await send_room_event_result(
            bot.client,
            claimed.room_id,
            claimed.event_type,
            content,
            transaction_id=claimed.transaction_id,
            operation="send_approval_delivery",
        )
        if not isinstance(response, nio.RoomSendResponse):
            msg = f"Matrix refused approval delivery {claimed.delivery_id!r}: {response}"
            raise ToolApprovalTransportError(msg)
        return str(response.event_id)

    async def resolve_approval_delivery(self, claimed: MatrixDelivery) -> str | None:
        """Adopt a card found after device change; edit misses remain safely replayable."""
        if claimed.edits_event_id is not None:
            return None
        approval_id = claimed.payload.get("approval_id")
        sender = self.transport_sender_id()
        if not isinstance(approval_id, str) or sender is None:
            return None
        return await self.locate_approval_card(claimed.room_id, sender, approval_id)

    async def locate_approval_card(
        self,
        room_id: str,
        card_sender: str,
        approval_id: str,
    ) -> str | None:
        """Find the Matrix event one unacknowledged card became."""
        bot = self.transport_bot(room_id)
        if bot is None or bot.client is None:
            msg = f"Router approval transport cannot read {room_id} to locate a card"
            raise ToolApprovalTransportError(msg)
        return await find_approval_card_event_id_via_room_messages(
            bot.client,
            room_id,
            card_sender=card_sender,
            approval_id=approval_id,
        )

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

    def reset_startup_cleanup_gate(self) -> None:
        """Reset one-shot startup approval cleanup state."""
        self._startup_router_ready_for_cleanup = False
        self._startup_runtime_support_ready_for_cleanup = False
        self._startup_cleanup_done = False
        self._startup_cleanup_retry_delay = _STARTUP_CLEANUP_INITIAL_RETRY_SECONDS
        self._startup_cleanup_attempts = 0
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None:
            retry.cancel()

    async def cancel_startup_cleanup_retry(self) -> None:
        """Cancel and await a delayed cleanup pass."""
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None and not retry.done():
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)

    async def close(self) -> None:
        """Release transport-owned tasks; the orchestrator owns the journal."""
        await self.cancel_startup_cleanup_retry()

    async def mark_startup_runtime_support_ready(self) -> None:
        """Record that startup cleanup may use runtime services."""
        self._startup_runtime_support_ready_for_cleanup = True
        await self._run_startup_cleanup_if_ready()

    async def handle_bot_ready(self, bot: _ApprovalTransportBot) -> None:
        """Record router first sync and attempt startup cleanup."""
        if bot.agent_name != ROUTER_AGENT_NAME or not bot.running or bot.client is None:
            return
        self._startup_router_ready_for_cleanup = True
        await self._run_startup_cleanup_if_ready()

    async def _run_startup_cleanup_if_ready(self) -> None:
        """Retry approval-card recovery and unavailable-owner cleanup until both finish."""
        if (
            self._startup_cleanup_done
            or not self._startup_router_ready_for_cleanup
            or not self._startup_runtime_support_ready_for_cleanup
        ):
            return
        async with self._startup_cleanup_lock:
            if self._startup_cleanup_done:
                return
            self._startup_cleanup_attempts += 1
            cards_recovered = await self._recover_approval_cards_on_startup()
            try:
                owners_settled = await self._reconcile_startup_unavailable()
            except Exception:
                logger.warning(
                    "tool_approval_unavailable_owner_cleanup_failed",
                    attempt=self._startup_cleanup_attempts,
                    exc_info=True,
                )
                owners_settled = False
            if not cards_recovered or not owners_settled:
                self._schedule_startup_cleanup_retry()
                return
            self._startup_cleanup_done = True
            self._retire_startup_cleanup_retry()

    async def _recover_approval_cards_on_startup(self) -> bool:
        """Recover current approval-card transport obligations."""
        try:
            sweep = await recover_approval_cards_on_startup()
        except Exception as exc:
            logger.warning(
                "tool_approval_startup_recovery_failed",
                error=str(exc),
                attempt=self._startup_cleanup_attempts,
                exc_info=True,
            )
            return False
        logger.info(
            "approval_startup_recovery_finished",
            attempt=self._startup_cleanup_attempts,
            scanned=sweep.scanned,
            retired=sweep.discarded,
            owed_count=sweep.failed,
        )
        if not sweep.complete:
            incomplete = (
                logger.error
                if self._startup_cleanup_attempts >= _STARTUP_CLEANUP_ATTEMPTS_BEFORE_ESCALATION
                else logger.warning
            )
            incomplete(
                "tool_approval_startup_recovery_incomplete",
                owed_count=sweep.failed,
                attempt=self._startup_cleanup_attempts,
            )
        return sweep.complete

    def _schedule_startup_cleanup_retry(self) -> None:
        """Arrange a later cleanup pass after a transient failure."""
        pending = self._startup_cleanup_retry
        if pending is not None and not pending.done() and pending is not asyncio.current_task():
            return
        self._startup_cleanup_retry = asyncio.create_task(
            self._run_startup_cleanup_after_delay(),
            name="approval_startup_cleanup_retry",
        )

    def _retire_startup_cleanup_retry(self) -> None:
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None and not retry.done() and retry is not asyncio.current_task():
            retry.cancel()

    async def _run_startup_cleanup_after_delay(self) -> None:
        delay = self._startup_cleanup_retry_delay
        self._startup_cleanup_retry_delay = min(delay * 2, _STARTUP_CLEANUP_MAX_RETRY_SECONDS)
        await asyncio.sleep(delay)
        await self._run_startup_cleanup_if_ready()
