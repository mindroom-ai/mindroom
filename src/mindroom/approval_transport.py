"""Matrix transport adapter for tool approval cards."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

import nio

from mindroom.approval_continuation import ApprovalContinuation, ApprovalContinuationStore, ApprovalDecision
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    can_send_to_encrypted_room,
    resolve_room_encryption_for_delivery,
    send_room_event_result,
)
from mindroom.matrix.large_messages import content_fits_normal_event, sidecar_upload_is_usable, upload_json_sidecar
from mindroom.matrix.message_builder import build_matrix_edit_content, build_message_content, build_thread_relation
from mindroom.matrix.room_history_reads import find_approval_card_event_id_via_room_messages
from mindroom.sync_bridge_state import is_loop_blocked_by_sync_tool_bridge
from mindroom.tool_approval import (
    DEFAULT_ROUTER_MANAGED_ROOM_REASON,
    SentApprovalEvent,
    ToolApprovalTransportError,
    expire_orphaned_approval_cards_on_startup,
    expire_suspended_tool_approval,
    initialize_approval_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalView

logger = get_logger(__name__)

_TApprovalTransportResult = TypeVar("_TApprovalTransportResult")

# How long a startup approval sweep that could not finish waits before asking
# again. Nothing else will trigger it: the gates that arm the sweep are startup
# events that have already happened, so a pass that gave up on a transient
# failure would leave answered cards clickable until the next restart.
_STARTUP_CLEANUP_INITIAL_RETRY_SECONDS = 1.0
_STARTUP_CLEANUP_MAX_RETRY_SECONDS = 30.0
# How many passes may come up short before the sweep stops saying so quietly.
# It keeps retrying past this: a pass cannot take a row that is still being
# published, so the cost of asking again is a read, while the cost of stopping
# is durable cleanup that no longer happens until the next restart -- and an
# outage that outlasts any fixed budget is exactly when cleanup is owed most.
# What changes is the volume, because a sweep still owed something this long
# after start is not waiting on anything transient.
_STARTUP_CLEANUP_ATTEMPTS_BEFORE_ESCALATION = 10
_CONTINUATION_DISPATCH_INITIAL_RETRY_SECONDS = 0.25
_CONTINUATION_DISPATCH_MAX_RETRY_SECONDS = 30.0
_CONTINUATION_EXPIRY_INITIAL_RETRY_SECONDS = 0.25
_CONTINUATION_EXPIRY_MAX_RETRY_SECONDS = 30.0


class _ApprovalTransportBot(Protocol):
    agent_name: str
    running: bool
    client: nio.AsyncClient | None

    @property
    def approval_room_ids(self) -> frozenset[str]:
        """Return rooms this bot durably owns for approval transport."""
        ...

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
    ) -> str | None:
        """Return the latest event id for one Matrix thread when known."""
        ...

    async def resume_approval_continuation(self, continuation: ApprovalContinuation) -> None: ...

    async def fail_approval_continuation(self, continuation: ApprovalContinuation, reason: str) -> None: ...


def _approval_relation_agent_name(content: dict[str, Any], *, fallback: str) -> str:
    agent_name = content.get("agent_name")
    return agent_name if isinstance(agent_name, str) and agent_name else fallback


async def _offload_oversized_full_arguments(
    client: nio.AsyncClient,
    room_id: str,
    send_content: dict[str, Any],
) -> dict[str, Any]:
    """Move full arguments that would overflow the card event into an uploaded JSON sidecar.

    A failed upload strips the payload and marks the card non-approvable so the manager's
    fail-closed resolution still holds: nothing approvable ships without complete arguments.
    """
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
    """Own Matrix delivery for tool approval cards and terminal edits."""

    runtime_paths: RuntimePaths
    bot_provider: Callable[[str], _ApprovalTransportBot | None]
    cards_provider: Callable[[], ApprovalView | None]
    entity_configured: Callable[[str], bool] | None = None
    _runtime_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _startup_router_ready_for_cleanup: bool = field(default=False, init=False, repr=False)
    _startup_runtime_support_ready_for_cleanup: bool = field(default=False, init=False, repr=False)
    _startup_cleanup_done: bool = field(default=False, init=False, repr=False)
    _continuations_recovered: bool = field(default=False, init=False, repr=False)
    _startup_cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _startup_cleanup_retry: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _startup_cleanup_retry_delay: float = field(
        default=_STARTUP_CLEANUP_INITIAL_RETRY_SECONDS,
        init=False,
        repr=False,
    )
    _continuations: ApprovalContinuationStore = field(init=False, repr=False)
    _continuation_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _expiry_tasks: dict[tuple[str, str], asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    _startup_cleanup_attempts: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        """Open the shared durable continuation coordinator."""
        self._continuations = ApprovalContinuationStore(self.runtime_paths.storage_root)

    def capture_runtime_loop(self) -> None:
        """Remember the runtime loop that owns Matrix client I/O."""
        runtime_loop = asyncio.get_running_loop()
        if self._runtime_loop is None:
            self._runtime_loop = runtime_loop
            return
        if self._runtime_loop is not runtime_loop:
            msg = "MindRoom runtime loop is already bound to a different event loop."
            raise RuntimeError(msg)

    def bind_approval_runtime(self) -> None:
        """Bind approval manager runtime hooks to the current Matrix transport."""
        initialize_approval_runtime(
            self.runtime_paths,
            sender=self.send_approval_event,
            editor=self.edit_approval_event,
            cards=self.cards_provider(),
            approval_room_ids=self.configured_approval_room_ids,
            transport_sender=self.transport_sender_id,
            sending_device=self.transport_device_id,
            locate_card=self.locate_approval_card,
            detached_decision_handler=self._handle_continuation_decision,
            detached_decision_ready=self._handle_continuation_decision_ready,
            detached_card_ready=self._handle_detached_card_ready,
        )

    async def _handle_detached_card_ready(
        self,
        approval_id: str,
        tool_call_id: str,
        card_event_id: str,
    ) -> bool:
        """Attach a delivered card even if its response coroutine was cancelled."""
        current = await asyncio.to_thread(self._continuations.get, approval_id)
        if current is None or current.state != "pending":
            return True
        call = next((call for call in current.calls if call.tool_call_id == tool_call_id), None)
        if call is None or call.decision_recorded:
            return True
        if call.card_event_id is not None:
            return call.card_event_id == card_event_id
        attached = await asyncio.to_thread(
            self._continuations.attach_card,
            approval_id,
            tool_call_id,
            card_event_id,
        )
        if attached is None:
            return False
        attached_call = next((call for call in attached.calls if call.tool_call_id == tool_call_id), None)
        return attached_call is not None and attached_call.card_event_id == card_event_id

    async def _handle_continuation_decision(
        self,
        approval_id: str,
        tool_call_id: str,
        status: Literal["approved", "denied", "expired"],
        reason: str | None,
    ) -> tuple[Literal["approved", "denied", "expired"], str | None]:
        decision = ApprovalDecision(status)
        continuation = await asyncio.to_thread(
            self._continuations.resolve_call,
            approval_id,
            tool_call_id,
            decision,
            reason=reason,
        )
        if continuation is None:
            return status, reason
        call = next((call for call in continuation.calls if call.tool_call_id == tool_call_id), None)
        if call is None or call.decision is None:
            return status, reason
        return call.decision.value, call.reason

    async def _handle_continuation_decision_ready(self, approval_id: str, tool_call_id: str) -> None:
        continuation = await asyncio.to_thread(self._continuations.acknowledge_call, approval_id, tool_call_id)
        if continuation is not None and continuation.state == "ready":
            self._schedule_continuation(continuation)

    def _schedule_continuation(self, continuation: ApprovalContinuation) -> None:
        if any(
            not task.done() and task.get_name() == f"approval-continuation-{continuation.approval_id}"
            for task in self._continuation_tasks
        ):
            return
        task = asyncio.create_task(
            self._dispatch_continuation(continuation.approval_id),
            name=f"approval-continuation-{continuation.approval_id}",
        )
        self._continuation_tasks.add(task)
        task.add_done_callback(self._finish_continuation_task)

    def _finish_continuation_task(self, task: asyncio.Task[None]) -> None:
        """Retire one dispatcher and observe failures from its detached task."""
        self._continuation_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "approval_continuation_dispatch_failed",
                error=str(error),
                exception_type=type(error).__name__,
            )

    async def _dispatch_continuation(self, approval_id: str) -> None:
        retry_seconds = _CONTINUATION_DISPATCH_INITIAL_RETRY_SECONDS
        waiting_logged = False
        while True:
            continuation = await asyncio.to_thread(self._continuations.get, approval_id)
            if continuation is None or continuation.state != "ready":
                return
            bot = self.bot_provider(continuation.entity_name)
            if bot is not None and bot.running:
                await bot.resume_approval_continuation(continuation)
                refreshed = await asyncio.to_thread(self._continuations.get, approval_id)
                if refreshed is None or refreshed.state != "ready":
                    return
                if not waiting_logged:
                    logger.warning(
                        "approval_continuation_resume_deferred",
                        approval_id=approval_id,
                        entity_name=continuation.entity_name,
                        retry_seconds=retry_seconds,
                    )
                    waiting_logged = True
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, _CONTINUATION_DISPATCH_MAX_RETRY_SECONDS)
                continue
            if self.entity_configured is None or self.entity_configured(continuation.entity_name):
                if not waiting_logged:
                    logger.warning(
                        "approval_continuation_waiting_for_owner",
                        approval_id=approval_id,
                        entity_name=continuation.entity_name,
                        retry_seconds=retry_seconds,
                    )
                    waiting_logged = True
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, _CONTINUATION_DISPATCH_MAX_RETRY_SECONDS)
                continue
            reason = f"Requesting agent '{continuation.entity_name}' is no longer available."
            fallback = self.bot_provider(ROUTER_AGENT_NAME)
            if fallback is not None and fallback.running:
                await fallback.fail_approval_continuation(continuation, reason)
                return
            if not waiting_logged:
                logger.warning(
                    "approval_continuation_waiting_for_router",
                    approval_id=approval_id,
                    entity_name=continuation.entity_name,
                    retry_seconds=retry_seconds,
                )
                waiting_logged = True
            await asyncio.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, _CONTINUATION_DISPATCH_MAX_RETRY_SECONDS)

    def _schedule_expiry(self, continuation: ApprovalContinuation) -> None:
        for call in continuation.calls:
            key = (continuation.approval_id, call.tool_call_id)
            if (
                (call.decision is not None and call.decision_recorded)
                or call.card_event_id is None
                or key in self._expiry_tasks
            ):
                continue

            async def expire(
                room_id: str = continuation.room_id,
                card_event_id: str = call.card_event_id,
                expires_at: str = call.expires_at,
                approval_id: str = continuation.approval_id,
                tool_call_id: str = call.tool_call_id,
            ) -> None:
                delay = max(
                    0.0,
                    (datetime.fromisoformat(expires_at).astimezone(UTC) - datetime.now(UTC)).total_seconds(),
                )
                await asyncio.sleep(delay)
                retry_seconds = _CONTINUATION_EXPIRY_INITIAL_RETRY_SECONDS
                while True:
                    try:
                        settled = await expire_suspended_tool_approval(room_id, card_event_id)
                    except Exception:
                        logger.warning(
                            "approval_continuation_expiry_retry",
                            approval_id=approval_id,
                            tool_call_id=tool_call_id,
                            retry_seconds=retry_seconds,
                            exc_info=True,
                        )
                        settled = False
                    if settled:
                        return
                    await asyncio.sleep(retry_seconds)
                    retry_seconds = min(retry_seconds * 2, _CONTINUATION_EXPIRY_MAX_RETRY_SECONDS)

            task = asyncio.create_task(
                expire(),
                name=f"approval-expiry-{continuation.approval_id}-{call.tool_call_id}",
            )
            self._expiry_tasks[key] = task
            task.add_done_callback(lambda completed, task_key=key: self._finish_expiry_task(task_key, completed))

    def _finish_expiry_task(self, key: tuple[str, str], task: asyncio.Task[None]) -> None:
        """Retire one expiry task and observe any Matrix settlement failure."""
        if self._expiry_tasks.get(key) is task:
            self._expiry_tasks.pop(key)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "approval_continuation_expiry_failed",
                approval_id=key[0],
                tool_call_id=key[1],
                error=str(error),
                exception_type=type(error).__name__,
            )

    async def _recover_continuations(self) -> bool:
        complete = True
        for continuation in await asyncio.to_thread(self._continuations.recoverable):
            if continuation.state == "publishing":
                settled = await self._fail_recovered_continuation(
                    continuation,
                    "Tool approval response publication was interrupted; the paused run was not replayed.",
                )
                complete = settled and complete
            elif continuation.state == "pending":
                recovered = await self._attach_recovered_cards(continuation)
                if any(not call.decision_recorded and call.card_event_id is None for call in recovered.calls):
                    settled = await self._fail_recovered_continuation(
                        recovered,
                        "Tool approval card was not delivered before the restart.",
                    )
                    complete = settled and complete
                    continue
                for call in recovered.calls:
                    if call.decision is not None and not call.decision_recorded and call.card_event_id is not None:
                        await expire_suspended_tool_approval(recovered.room_id, call.card_event_id)
                refreshed = await asyncio.to_thread(self._continuations.get, recovered.approval_id)
                if refreshed is None or refreshed.state != "pending":
                    continue
                self._schedule_expiry(refreshed)
            elif continuation.state == "ready":
                self._schedule_continuation(continuation)
            else:
                settled = await self._fail_recovered_continuation(
                    continuation,
                    "Tool approval continuation was interrupted after it was claimed; it was not replayed.",
                )
                complete = settled and complete
        return complete

    async def _attach_recovered_cards(self, continuation: ApprovalContinuation) -> ApprovalContinuation:
        """Repair the crash window between durable card delivery and continuation attachment."""
        missing_ids = {
            call.tool_call_id
            for call in continuation.calls
            if not call.decision_recorded and call.card_event_id is None
        }
        cards = self.cards_provider()
        if not missing_ids or cards is None:
            return continuation
        cursor: tuple[int, str] | None = None
        while missing_ids:
            page = await cards.pending_approval_cards(room_id=continuation.room_id, limit=256, after=cursor)
            if not page:
                break
            cursor = (page[-1].created_at_ns, page[-1].transaction_id)
            for stored in page:
                content = stored.card.get("content")
                if not isinstance(content, dict) or content.get("continuation_id") != continuation.approval_id:
                    continue
                tool_call_id = content.get("tool_call_id")
                if not isinstance(tool_call_id, str) or tool_call_id not in missing_ids or stored.card_event_id is None:
                    continue
                attached = await asyncio.to_thread(
                    self._continuations.attach_card,
                    continuation.approval_id,
                    tool_call_id,
                    stored.card_event_id,
                )
                if attached is not None:
                    continuation = attached
                    missing_ids.discard(tool_call_id)
        return continuation

    async def _fail_recovered_continuation(self, continuation: ApprovalContinuation, reason: str) -> bool:
        """Terminalize one continuation and edit its waiting response when possible."""
        entity_bot = self.bot_provider(continuation.entity_name)
        router_bot = self.bot_provider(ROUTER_AGENT_NAME)
        bot = entity_bot if entity_bot is not None and entity_bot.running else router_bot
        if bot is None or not bot.running:
            return False
        terminalized = await asyncio.gather(
            *(
                expire_suspended_tool_approval(continuation.room_id, call.card_event_id)
                for call in continuation.calls
                if not call.decision_recorded and call.card_event_id is not None
            ),
            return_exceptions=True,
        )
        if any(result is not True for result in terminalized):
            logger.warning(
                "approval_continuation_card_settlement_incomplete",
                approval_id=continuation.approval_id,
            )
            return False
        await bot.fail_approval_continuation(continuation, reason)
        refreshed = await asyncio.to_thread(self._continuations.get, continuation.approval_id)
        return refreshed is not None and refreshed.state == "failed"

    async def _run_on_runtime_loop(
        self,
        coroutine_factory: Callable[[], Coroutine[Any, Any, _TApprovalTransportResult]],
    ) -> _TApprovalTransportResult:
        """Run one coroutine on the runtime loop that owns Matrix client I/O."""
        runtime_loop = self._runtime_loop
        if runtime_loop is None or runtime_loop.is_closed():
            msg = "Approval runtime loop is not available."
            raise RuntimeError(msg)

        current_loop = asyncio.get_running_loop()
        if current_loop is runtime_loop:
            return await coroutine_factory()

        if is_loop_blocked_by_sync_tool_bridge(runtime_loop):
            msg = (
                "Cannot perform Matrix approval transport while synchronous FunctionCall.execute() "
                "is blocking the MindRoom runtime loop; use FunctionCall.aexecute() or run execute() "
                "outside the runtime event loop."
            )
            raise ToolApprovalTransportError(msg)

        future = asyncio.run_coroutine_threadsafe(coroutine_factory(), runtime_loop)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise

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
            resolved_latest_event_id = await bot.latest_thread_event_id_if_needed(room_id, thread_id)
            if resolved_latest_event_id is not None:
                latest_thread_event_id = resolved_latest_event_id
        return build_thread_relation(
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
        )

    async def send_approval_event(
        self,
        room_id: str,
        thread_id: str | None,
        content: dict[str, Any],
        transaction_id: str,
    ) -> SentApprovalEvent | None:
        """Send one custom approval event into the active Matrix thread."""
        return await self._run_on_runtime_loop(
            lambda: self.send_approval_event_now(room_id, thread_id, content, transaction_id),
        )

    async def send_approval_event_now(
        self,
        room_id: str,
        thread_id: str | None,
        content: dict[str, Any],
        transaction_id: str,
    ) -> SentApprovalEvent | None:
        """Send one custom approval event on the current loop.

        The transaction is the caller's, not a fresh one per attempt, so a send
        repeated after a crash collapses onto the event the homeserver already
        accepted instead of putting a second card in the room.
        """
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
        send_content = await _offload_oversized_full_arguments(bot.client, room_id, send_content)
        response = await send_room_event_result(
            bot.client,
            room_id,
            "io.mindroom.tool_approval",
            send_content,
            transaction_id=transaction_id,
            operation="send_approval_event",
        )
        if isinstance(response, nio.RoomSendResponse):
            sender_user_id = bot.client.user_id
            if not isinstance(sender_user_id, str) or not sender_user_id:
                logger.warning(
                    "Approval sender bot is missing a Matrix user id",
                    room_id=room_id,
                    thread_id=thread_id,
                    agent_name=bot.agent_name,
                )
            return SentApprovalEvent(event_id=str(response.event_id), sent_content=send_content)
        logger.warning(
            "Failed to send approval Matrix event",
            room_id=room_id,
            thread_id=thread_id,
            agent_name=bot.agent_name,
            response=str(response),
        )
        return None

    async def locate_approval_card(
        self,
        room_id: str,
        card_sender: str,
        approval_id: str,
    ) -> str | None:
        """Find the Matrix event one unacknowledged approval card became."""
        return await self._run_on_runtime_loop(
            lambda: self.locate_approval_card_now(room_id, card_sender, approval_id),
        )

    async def locate_approval_card_now(
        self,
        room_id: str,
        card_sender: str,
        approval_id: str,
    ) -> str | None:
        """Read the room for one approval card on the current loop.

        Raising and returning None mean different things to the caller: None is
        the room's answer that no such card exists, and an exception says the
        question could not be put. So a transport that cannot read the room
        raises rather than reporting an absence it did not establish -- a
        wrong absence there retires the row, and the card it belongs to stays
        clickable with nothing behind it forever.
        """
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

    async def edit_approval_event(
        self,
        room_id: str,
        event_id: str,
        new_content: dict[str, Any],
    ) -> bool:
        """Edit one previously sent approval event."""
        return await self._run_on_runtime_loop(
            lambda: self.edit_approval_event_now(
                room_id,
                event_id,
                new_content,
            ),
        )

    def _bot_has_approval_room(
        self,
        bot: _ApprovalTransportBot,
        room_id: str,
    ) -> bool:
        """Return whether one bot can safely post into an approval room."""
        return bot.client is not None and room_id in bot.approval_room_ids

    def transport_bot(
        self,
        room_id: str,
    ) -> _ApprovalTransportBot | None:
        """Return the live router bot that owns approval transport for one room."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None or not bot.running or bot.client is None:
            return None
        if not self._bot_has_approval_room(bot, room_id):
            return None
        return bot

    def transport_sender_id(self) -> str | None:
        """Return the Matrix user id that owns approval cards for this runtime."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None or bot.client is None:
            return None
        user_id = bot.client.user_id
        return user_id if isinstance(user_id, str) and user_id else None

    def transport_device_id(self) -> str | None:
        """Return the Matrix device that sends approval cards for this runtime.

        The transaction IDs the recovery pass relies on belong to this device,
        so a card claimed under a different one cannot be presented again.
        """
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        if bot is None or bot.client is None:
            return None
        device_id = bot.client.device_id
        return device_id if isinstance(device_id, str) and device_id else None

    def configured_approval_room_ids(self) -> set[str]:
        """Return rooms currently served by the router approval transport."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        return set() if bot is None or bot.client is None else set(bot.approval_room_ids)

    async def edit_approval_event_now(
        self,
        room_id: str,
        event_id: str,
        new_content: dict[str, Any],
    ) -> bool:
        """Edit one previously sent approval event on the current loop."""
        bot = self.transport_bot(room_id)
        if bot is None or bot.client is None:
            return False
        if not can_send_to_encrypted_room(bot.client, room_id, operation="edit_approval_event"):
            return False

        replacement_content = {key: value for key, value in new_content.items() if key != "thread_id"}
        response = await send_room_event_result(
            bot.client,
            room_id,
            "io.mindroom.tool_approval",
            build_matrix_edit_content(event_id, replacement_content),
            operation="edit_approval_event",
        )
        if not isinstance(response, nio.RoomSendResponse):
            logger.warning(
                "Failed to edit approval Matrix event",
                room_id=room_id,
                event_id=event_id,
                agent_name=bot.agent_name,
                response=str(response),
            )
            return False
        return True

    async def send_notice(
        self,
        *,
        room_id: str,
        approval_event_id: str,
        thread_id: str | None,
        reason: str,
    ) -> bool:
        """Send one approval notice through the router transport bot."""
        bot = self.transport_bot(room_id)
        if bot is None or bot.client is None:
            logger.warning(
                "Router approval transport unavailable for notice",
                room_id=room_id,
                approval_event_id=approval_event_id,
            )
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
            operation="send_approval_notice",
        )
        if isinstance(response, nio.RoomSendResponse):
            return True

        logger.warning(
            "Failed to send approval notice",
            room_id=room_id,
            approval_event_id=approval_event_id,
            agent_name=bot.agent_name,
            response=str(response),
        )
        return False

    def reset_startup_cleanup_gate(self) -> None:
        """Reset one-shot startup approval cleanup state for a fresh runtime start."""
        self._startup_router_ready_for_cleanup = False
        self._startup_runtime_support_ready_for_cleanup = False
        self._startup_cleanup_done = False
        self._continuations_recovered = False
        self._startup_cleanup_retry_delay = _STARTUP_CLEANUP_INITIAL_RETRY_SECONDS
        self._startup_cleanup_attempts = 0
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None:
            retry.cancel()

    async def cancel_startup_cleanup_retry(self) -> None:
        """Await the cancellation of a sweep still waiting to try again.

        A retry sleeps for up to half a minute, which is long enough to outlive
        an orderly shutdown and be torn down as a pending task instead.
        """
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        tasks: list[asyncio.Task[None]] = [*self._continuation_tasks, *self._expiry_tasks.values()]
        if retry is not None and not retry.done():
            retry.cancel()
            tasks.append(retry)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._continuation_tasks.clear()
        self._expiry_tasks.clear()

    async def close(self) -> None:
        """Close the transport-owned continuation-store handle."""
        await asyncio.to_thread(self._continuations.close)

    async def mark_startup_runtime_support_ready(self) -> None:
        """Record that approval runtime support can now perform startup cleanup."""
        self._startup_runtime_support_ready_for_cleanup = True
        await self._run_startup_cleanup_if_ready()

    async def handle_bot_ready(self, bot: _ApprovalTransportBot) -> None:
        """Record router first sync and run startup approval cleanup once all gates are ready."""
        if bot.agent_name != ROUTER_AGENT_NAME or not bot.running or bot.client is None:
            return
        self._startup_router_ready_for_cleanup = True
        await self._run_startup_cleanup_if_ready()

    async def _run_startup_cleanup_if_ready(self) -> None:
        """Run the startup approval sweep once it can run, and until it finishes.

        Marked done only by a sweep that settled everything it found. A card it
        could not settle is still in the room and still clickable, with nothing
        live behind it to answer the click -- and the gates that arm this sweep
        are startup events that will not happen a second time. So a pass that
        came up short arranges the next one itself.
        """
        if (
            self._startup_cleanup_done
            or not self._startup_router_ready_for_cleanup
            or not self._startup_runtime_support_ready_for_cleanup
        ):
            return
        async with self._startup_cleanup_lock:
            if (
                self._startup_cleanup_done
                or not self._startup_router_ready_for_cleanup
                or not self._startup_runtime_support_ready_for_cleanup
            ):
                return
            self._startup_cleanup_attempts += 1
            if not await self._discard_orphaned_approval_cards_on_startup():
                self._schedule_startup_cleanup_retry()
                return
            if not self._continuations_recovered:
                if not await self._recover_continuations():
                    self._schedule_startup_cleanup_retry()
                    return
                self._continuations_recovered = True
            self._startup_cleanup_done = True
            self._retire_startup_cleanup_retry()

    async def _discard_orphaned_approval_cards_on_startup(self) -> bool:
        """Discard orphaned approval cards, reporting whether any are still owed."""
        try:
            sweep = await expire_orphaned_approval_cards_on_startup()
        except Exception as exc:
            logger.warning(
                "tool_approval_startup_discard_failed",
                error=str(exc),
                attempt=self._startup_cleanup_attempts,
                exc_info=True,
            )
            return False
        # Said unconditionally, because the healthy outcome of the guards this
        # sweep runs under is a pass that settles nothing, and a pass that
        # settled nothing has to be distinguishable from one that never ran.
        logger.info(
            "approval_startup_sweep_finished",
            attempt=self._startup_cleanup_attempts,
            scanned=sweep.scanned,
            discarded=sweep.discarded,
            owed_count=sweep.failed,
            skipped_in_flight=sweep.skipped_in_flight,
            skipped_live_waiter=sweep.skipped_live_waiter,
            dropped_unrecoverable=sweep.dropped_unrecoverable,
            kept_unusable=sweep.kept_unusable,
            dropped_never_attempted=sweep.dropped_never_attempted,
        )
        if not sweep.complete:
            incomplete = (
                logger.error
                if self._startup_cleanup_attempts >= _STARTUP_CLEANUP_ATTEMPTS_BEFORE_ESCALATION
                else logger.warning
            )
            incomplete(
                "tool_approval_startup_discard_incomplete",
                owed_count=sweep.failed,
                attempt=self._startup_cleanup_attempts,
            )
        return sweep.complete

    def _schedule_startup_cleanup_retry(self) -> None:
        """Arrange one later sweep, since no startup gate will fire again.

        The guard is there so two different callers cannot each arm a task. It
        deliberately does not count the caller's own retry: a retry runs the
        sweep itself, so the pass that discovers another attempt is owed is
        always running inside the very task a plain "is one live?" check would
        find. Counting it would let a retry block its own successor, and the
        whole backoff would collapse into one extra attempt -- which is the
        failure this retry exists to prevent, arriving one round later.
        """
        pending = self._startup_cleanup_retry
        if pending is not None and not pending.done() and pending is not asyncio.current_task():
            return
        self._startup_cleanup_retry = asyncio.create_task(
            self._run_startup_cleanup_after_delay(),
            name="approval_startup_cleanup_retry",
        )

    def _retire_startup_cleanup_retry(self) -> None:
        """Drop a waiting retry the finished sweep has made pointless.

        Cancelled rather than merely forgotten, because a forgotten task is one
        no shutdown can reach. The caller's own task is exempt: it is finishing
        anyway, and cancelling it here would cancel the sweep reporting success.
        """
        retry = self._startup_cleanup_retry
        self._startup_cleanup_retry = None
        if retry is not None and not retry.done() and retry is not asyncio.current_task():
            retry.cancel()

    async def _run_startup_cleanup_after_delay(self) -> None:
        delay = self._startup_cleanup_retry_delay
        self._startup_cleanup_retry_delay = min(delay * 2, _STARTUP_CLEANUP_MAX_RETRY_SECONDS)
        await asyncio.sleep(delay)
        await self._run_startup_cleanup_if_ready()
