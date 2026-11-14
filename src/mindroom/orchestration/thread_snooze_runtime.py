"""Thread snooze runtime binding for the orchestrator: room state in, wake messages out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import nio
from pydantic import ValidationError

from mindroom.background_tasks import SupervisedLoop
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.requester_identity import is_human_requester_id
from mindroom.thread_snooze import (
    THREAD_SNOOZE_CONTENT_KEY,
    Snooze,
    SnoozeUnavailableError,
    ThreadSnoozeRuntime,
    snooze_from_state_event,
    wake_message_body,
)
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    SNOOZED_THREAD_TAG,
    ThreadTagRecord,
    ThreadTagsPermissionError,
    get_thread_tag_content,
    remove_thread_tag,
    snooze_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from datetime import datetime
    from typing import Any

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


logger = get_logger(__name__)


@dataclass
class ThreadSnoozeRuntimeCoordinator:
    """Feed the snooze index from room state and live tag events, and wake threads through the running bots."""

    name = "thread_snooze"
    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config]
    bots: Callable[[], Mapping[str, AgentBot]]
    _runtime: ThreadSnoozeRuntime = field(init=False)
    _loop: SupervisedLoop = field(init=False)
    _restored_rooms: set[str] = field(default_factory=set, init=False)
    _restore_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Create the snooze index and its supervised wake loop."""
        self._runtime = ThreadSnoozeRuntime(self)
        self._loop = SupervisedLoop("thread_snooze", self._runtime.run)

    async def sync(self, config: Config) -> None:  # noqa: ARG002 - snoozes need no config to wake
        """Start the wake loop."""
        self._loop.start()

    async def stop(self) -> None:
        """Stop the wake loop."""
        await self._loop.stop()

    def snoozed(self, room_id: str, thread_root_id: str, *, until: datetime, note: str | None, set_by: str) -> None:
        """Index a snooze this runtime just wrote, mentioning its setter on wake when that is a person."""
        mention = None if self._is_bot(set_by) else set_by
        self._runtime.track(Snooze(room_id, thread_root_id, until, note, mention))

    def unsnoozed(self, room_id: str, thread_root_id: str) -> None:  # privata: ignore - the thread snooze tool calls it
        """Drop a snooze this runtime just removed."""
        self._runtime.forget(room_id, thread_root_id)

    def observe_state_event(self, room_id: str, source: Mapping[str, object]) -> None:
        """Index or drop the snooze a raw thread tag state event sets or removes; ignore any other event."""
        parsed = snooze_from_state_event(room_id, source, is_bot=self._is_bot)
        if parsed is None:
            return
        thread_root_id, snooze = parsed
        if snooze is None:
            self._runtime.forget(room_id, thread_root_id)
        else:
            self._runtime.track(snooze)

    async def restore_room(self, bot: AgentBot, room_id: str) -> None:
        """Index the snoozes in a room's state the first time any bot joins it in this process."""
        client = bot.client
        if client is None or room_id in self._restored_rooms:
            return
        async with self._restore_locks.setdefault(room_id, asyncio.Lock()):
            if room_id in self._restored_rooms:
                return
            try:
                response = await client.room_get_state(room_id)
                if not isinstance(response, nio.RoomGetStateResponse):
                    logger.warning("thread_snooze_room_state_unread", room_id=room_id, error=str(response))
                    return
                self._restored_rooms.add(room_id)
                for event in response.events:
                    self.observe_state_event(room_id, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("thread_snooze_restore_failed", agent=bot.agent_name, room_id=room_id)

    def _is_bot(self, user_id: str) -> bool:
        return not is_human_requester_id(user_id, self.config_provider(), self.runtime_paths)

    def _joined_bots(self, room_id: str) -> Iterator[tuple[AgentBot, nio.AsyncClient]]:
        """Yield the bots joined to a room with their clients, the router first and the rest by name."""
        bots = self.bots()
        for name in sorted(bots, key=lambda name: (name != ROUTER_AGENT_NAME, name)):
            bot = bots[name]
            client = bot.client
            if client is not None and room_id in client.rooms:
                yield bot, client

    async def _read_tag(self, room_id: str, thread_root_id: str, tag: str) -> dict[str, Any] | None:
        joined = next(self._joined_bots(room_id), None)
        if joined is None:
            msg = f"No bot is joined to {room_id}"
            raise SnoozeUnavailableError(msg)
        try:
            return await get_thread_tag_content(joined[1], room_id, thread_root_id, tag)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("thread_snooze_tag_unread", room_id=room_id, thread_root_id=thread_root_id, exc_info=True)
            raise SnoozeUnavailableError(str(exc)) from exc

    async def current_until(self, room_id: str, thread_root_id: str) -> datetime | None:
        """Return the wake time now in room state, or None when the snooze is gone."""
        content = await self._read_tag(room_id, thread_root_id, SNOOZED_THREAD_TAG)
        if content is None:
            return None
        try:
            return snooze_until(ThreadTagRecord.model_validate(content))
        except ValidationError:
            return None

    async def is_resolved(self, room_id: str, thread_root_id: str) -> bool:
        """Return whether the thread carries the resolved tag."""
        return await self._read_tag(room_id, thread_root_id, RESOLVED_THREAD_TAG) is not None

    async def clear(self, room_id: str, thread_root_id: str) -> bool:
        """Remove the snooze through the first joined bot allowed to; False when none is."""
        for bot, client in self._joined_bots(room_id):
            try:
                await remove_thread_tag(client, room_id, thread_root_id, SNOOZED_THREAD_TAG)
            except ThreadTagsPermissionError:
                logger.info("thread_snooze_clear_denied", agent=bot.agent_name, room_id=room_id)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "thread_snooze_clear_failed",
                    room_id=room_id,
                    thread_root_id=thread_root_id,
                    exc_info=True,
                )
                raise SnoozeUnavailableError(str(exc)) from exc
            return True
        return False

    async def send_ended(self, snooze: Snooze) -> bool:
        """Post the wake message into the thread as the first joined bot; False when it was not delivered."""
        joined = next(self._joined_bots(snooze.room_id), None)
        if joined is None:
            return False
        try:
            event_id = await joined[0].hook_context.send_message(
                snooze.room_id,
                wake_message_body(snooze.note),
                snooze.thread_root_id,
                "thread_snooze",
                {THREAD_SNOOZE_CONTENT_KEY: {"event": "ended", "until": snooze.until.isoformat()}},
                trigger_dispatch=False,
                mentions=[snooze.mention] if snooze.mention is not None else [],
                wake_mentioned=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The runtime retries an undelivered wake message; it already removed the snooze, so nothing else would.
            logger.warning(
                "thread_snooze_send_failed",
                room_id=snooze.room_id,
                thread_root_id=snooze.thread_root_id,
                exc_info=True,
            )
            return False
        return event_id is not None
