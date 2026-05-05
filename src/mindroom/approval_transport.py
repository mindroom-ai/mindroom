"""Matrix transport adapter for tool approval cards."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from math import ceil
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import nio

from mindroom.config.matrix import ignore_unverified_devices_for_config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.cache import normalize_nio_event_for_cache
from mindroom.matrix.client_delivery import can_send_to_encrypted_room
from mindroom.matrix.message_builder import build_matrix_edit_content, build_message_content, build_thread_relation
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

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache

logger = get_logger(__name__)

_TApprovalTransportResult = TypeVar("_TApprovalTransportResult")


class _ApprovalTransportBot(Protocol):
    agent_name: str
    running: bool
    client: nio.AsyncClient | None
    event_cache: ConversationEventCache

    async def latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str = "agent_bot_latest_thread_event_lookup",
    ) -> str | None:
        """Return the latest event id for one Matrix thread when known."""
        ...


def _approval_startup_lookback_hours(config: Config) -> int:
    """Return the cache lookback window needed to clean up live-only approval cards."""
    timeout_days = config.tool_approval.timeout_days
    for rule in config.tool_approval.rules:
        if rule.timeout_days is not None:
            timeout_days = max(timeout_days, rule.timeout_days)
    return max(1, ceil(timeout_days * 24))


def _approval_relation_agent_name(content: dict[str, Any], *, fallback: str) -> str:
    agent_name = content.get("agent_name")
    return agent_name if isinstance(agent_name, str) and agent_name else fallback


@dataclass
class ApprovalMatrixTransport:
    """Own Matrix delivery for tool approval cards and terminal edits."""

    runtime_paths: RuntimePaths
    bot_provider: Callable[[str], _ApprovalTransportBot | None]
    config_provider: Callable[[], Config | None]
    event_cache_provider: Callable[[], ConversationEventCache]
    _runtime_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _cache_write_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)

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
            event_cache=self.event_cache_provider(),
            approval_room_ids=self.configured_approval_room_ids,
            transport_sender=self.transport_sender_id,
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
            resolved_latest_event_id = await bot.latest_thread_event_id_if_needed(
                room_id,
                thread_id,
                caller_label="approval_transport_thread_relation",
            )
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
    ) -> SentApprovalEvent | None:
        """Send one custom approval event into the active Matrix thread."""
        return await self._run_on_runtime_loop(
            lambda: self.send_approval_event_now(room_id, thread_id, content),
        )

    async def send_approval_event_now(
        self,
        room_id: str,
        thread_id: str | None,
        content: dict[str, Any],
    ) -> SentApprovalEvent | None:
        """Send one custom approval event on the current loop."""
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
        response = await bot.client.room_send(
            room_id=room_id,
            message_type="io.mindroom.tool_approval",
            content=send_content,
            ignore_unverified_devices=self._ignore_unverified_devices(),
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
            self.track_cache_write(bot, room_id, str(response.event_id))
            return SentApprovalEvent(event_id=str(response.event_id))
        logger.warning(
            "Failed to send approval Matrix event",
            room_id=room_id,
            thread_id=thread_id,
            agent_name=bot.agent_name,
            response=str(response),
        )
        return None

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
        if bot.client is None:
            return False
        return room_id in tuple(bot.client.rooms)

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

    def configured_approval_room_ids(self) -> set[str]:
        """Return rooms currently served by the router approval transport."""
        bot = self.bot_provider(ROUTER_AGENT_NAME)
        room_ids: set[str] = set()
        if bot is not None and bot.client is not None:
            room_ids.update(bot.client.rooms)
        return room_ids

    def _ignore_unverified_devices(self) -> bool:
        """Return the active Matrix delivery trust policy for approval sends."""
        config = self.config_provider()
        if config is None:
            msg = "Approval Matrix transport requires an active config."
            raise ToolApprovalTransportError(msg)
        return ignore_unverified_devices_for_config(config)

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

        thread_id = new_content.get("thread_id")
        if thread_id is not None and not isinstance(thread_id, str):
            msg = "Approval thread_id must be a string when present."
            raise TypeError(msg)

        replacement_content = {key: value for key, value in new_content.items() if key != "thread_id"}
        if isinstance(thread_id, str) and thread_id:
            replacement_content["m.relates_to"] = await self._approval_thread_relation(
                room_id,
                thread_id,
                _approval_relation_agent_name(new_content, fallback=bot.agent_name),
            )
        response = await bot.client.room_send(
            room_id=room_id,
            message_type="io.mindroom.tool_approval",
            content=build_matrix_edit_content(event_id, replacement_content),
            ignore_unverified_devices=self._ignore_unverified_devices(),
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
        await self.cache_approval_event_now(bot, room_id, str(response.event_id))
        return True

    def track_cache_write(self, bot: _ApprovalTransportBot, room_id: str, event_id: str) -> None:
        """Cache an outbound approval event in the background."""
        task = asyncio.create_task(
            self.cache_approval_event_now(bot, room_id, event_id),
            name=f"approval_cache_write_{event_id}",
        )
        self._cache_write_tasks.add(task)
        task.add_done_callback(self._finish_cache_write)

    def _finish_cache_write(self, task: asyncio.Task[None]) -> None:
        self._cache_write_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            logger.warning("approval_cache_write_failed", error=str(exc))

    async def cache_approval_event_now(
        self,
        bot: _ApprovalTransportBot,
        room_id: str,
        event_id: str,
    ) -> None:
        """Store a freshly sent approval event after Matrix assigns canonical event fields."""
        if bot.client is None:
            return
        try:
            response = await bot.client.room_get_event(room_id, event_id)
            if not isinstance(response, nio.RoomGetEventResponse):
                return
            await bot.event_cache.store_event(
                event_id,
                room_id,
                normalize_nio_event_for_cache(response.event, event_id=event_id),
            )
        except Exception as exc:
            logger.warning(
                "Failed to cache outbound approval event",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )

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
        response = await bot.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=self._ignore_unverified_devices(),
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

    async def handle_bot_ready(self, bot: _ApprovalTransportBot) -> None:
        """Discard orphaned approval cards once the router finishes first sync."""
        if bot.agent_name != ROUTER_AGENT_NAME or not bot.running or bot.client is None:
            return
        config = self.config_provider()
        if config is None:
            return
        try:
            discarded_count = await expire_orphaned_approval_cards_on_startup(
                lookback_hours=_approval_startup_lookback_hours(config),
            )
        except Exception as exc:
            logger.warning("tool_approval_startup_discard_failed", error=str(exc))
            return
        if discarded_count > 0:
            logger.info("approval.startup_discard", discarded_count=discarded_count)
