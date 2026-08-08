"""Matrix transport adapter for tool approval cards."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import nio

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
    _runtime_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
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
        )

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
        self._startup_cleanup_retry_delay = _STARTUP_CLEANUP_INITIAL_RETRY_SECONDS
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
        if retry is None or retry.done():
            return
        retry.cancel()
        with suppress(asyncio.CancelledError):
            await retry

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
            if not await self._discard_orphaned_approval_cards_on_startup():
                self._schedule_startup_cleanup_retry()
                return
            self._startup_cleanup_done = True
            self._retire_startup_cleanup_retry()

    async def _discard_orphaned_approval_cards_on_startup(self) -> bool:
        """Discard orphaned approval cards, reporting whether any are still owed."""
        try:
            sweep = await expire_orphaned_approval_cards_on_startup()
        except Exception as exc:
            logger.warning("tool_approval_startup_discard_failed", error=str(exc))
            return False
        if sweep.discarded > 0:
            logger.info("approval.startup_discard", discarded_count=sweep.discarded)
        if not sweep.complete:
            logger.warning("tool_approval_startup_discard_incomplete", owed_count=sweep.failed)
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
