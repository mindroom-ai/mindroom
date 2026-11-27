"""Orchestrator runtime coordinator that automatically reopens resolved threads on new replies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.requester_identity import is_persisted_managed_entity_id
from mindroom.room_steward import room_steward
from mindroom.thread_auto_reopen import extract_auto_reopen_reply
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagsPermissionError,
    get_thread_tag_content,
    parse_thread_tag_record,
    remove_thread_tag,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import nio
    import structlog

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.thread_tags import ThreadTagRecord

logger = get_logger(__name__)

_MAX_SEEN_EVENTS = 2048
type _ThreadKey = tuple[str, str]
type _JoinedBot = tuple[AgentBot, nio.AsyncClient]


@dataclass
class ThreadAutoReopenCoordinator:
    """Clear a thread's ``resolved`` tag when a newer qualifying thread reply arrives."""

    name = "thread_auto_reopen"
    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config]
    bots: Callable[[], Mapping[str, AgentBot]]
    retry_delays: tuple[float, ...] = (1.0, 3.0)
    _seen_events: dict[tuple[str, str], None] = field(default_factory=dict, init=False)
    _pending_ts: dict[_ThreadKey, int] = field(default_factory=dict, init=False)
    _in_flight: dict[_ThreadKey, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _permission_warned_rooms: set[str] = field(default_factory=set, init=False)
    _stopped: bool = field(default=False, init=False)

    async def sync(self, config: Config) -> None:  # noqa: ARG002 - reads live config on demand
        """Mark the coordinator active."""
        self._stopped = False

    async def stop(self) -> None:
        """Stop scheduling checks and cancel all in-flight background reopen tasks."""
        self._stopped = True
        self._pending_ts.clear()
        tasks = list(self._in_flight.values())
        self._in_flight.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def observe_timeline_event(self, room_id: str, source: Mapping[str, object]) -> None:
        """Inspect one newly admitted non-history cleartext timeline event and schedule reopen if needed."""
        if self._stopped:
            return
        reply = extract_auto_reopen_reply(source, sender_is_managed=self._is_managed_sender)
        if reply is None:
            return
        root, event_id, origin_server_ts = reply
        if (event_key := (room_id, event_id)) in self._seen_events:
            return
        if len(self._seen_events) >= _MAX_SEEN_EVENTS:
            del self._seen_events[next(iter(self._seen_events))]
        self._seen_events[event_key] = None

        key = (room_id, root)
        self._pending_ts[key] = max(self._pending_ts.get(key, -1), origin_server_ts)
        if (existing := self._in_flight.get(key)) is not None and not existing.done():
            return
        self._in_flight[key] = asyncio.create_task(self._process_thread(key), name=f"thread_auto_reopen:{room_id}")

    async def _process_thread(self, key: _ThreadKey) -> None:
        log = logger.bind(room_id=key[0], thread_root_id=key[1])
        try:
            while not self._stopped and (reply_ts := self._pending_ts.pop(key, None)) is not None:
                for delay in (0.0, *self.retry_delays):
                    if delay > 0:
                        await asyncio.sleep(delay)
                        reply_ts = max(reply_ts, self._pending_ts.pop(key, -1))
                    if self._stopped or await self._attempt_reopen(key, reply_ts, log):
                        break
                else:
                    log.warning("thread_auto_reopen_exhausted", attempts=1 + len(self.retry_delays))
        finally:
            if self._in_flight.get(key) is asyncio.current_task():
                del self._in_flight[key]

    async def _attempt_reopen(self, key: _ThreadKey, reply_ts: int, log: structlog.stdlib.BoundLogger) -> bool:
        """Check ``resolved`` via one GET and clear if older than ``reply_ts``; return False to retry."""
        room_id, root = key
        joined = self._joined_bots(room_id)
        if not joined:
            return True
        reader_bot, reader_client = joined[0]
        try:
            content = await get_thread_tag_content(reader_client, room_id, root, RESOLVED_THREAD_TAG)
        except Exception as exc:
            log.warning("thread_auto_reopen_read_failed", agent=reader_bot.agent_name, error=str(exc))
            return False
        record = None if content is None else parse_thread_tag_record(RESOLVED_THREAD_TAG, content)
        if record is None or reply_ts <= int(record.set_at.timestamp() * 1000):
            return True
        return await self._clear_resolved(joined, key, record, log)

    async def _clear_resolved(
        self,
        joined: list[_JoinedBot],
        key: _ThreadKey,
        record: ThreadTagRecord,
        log: structlog.stdlib.BoundLogger,
    ) -> bool:
        room_id, root = key
        saw_transient = False
        for bot, client in joined:
            room = client.rooms.get(room_id)
            if room is None or not room.power_levels.can_user_send_state(client.user_id, THREAD_TAGS_EVENT_TYPE):
                continue
            try:
                state = await remove_thread_tag(client, room_id, root, RESOLVED_THREAD_TAG, expected_record=record)
            except ThreadTagsPermissionError:
                log.info("thread_auto_reopen_clear_denied", agent=bot.agent_name)
                continue
            except Exception as exc:
                log.warning("thread_auto_reopen_clear_failed", agent=bot.agent_name, error=str(exc))
                saw_transient = True
                continue
            self._permission_warned_rooms.discard(room_id)
            cleared = RESOLVED_THREAD_TAG not in state.tags
            event = "thread_auto_reopened" if cleared else "thread_auto_reopen_changed_before_clear"
            log.info(event, agent=bot.agent_name)
            return True
        if saw_transient:
            return False
        if room_id not in self._permission_warned_rooms:
            self._permission_warned_rooms.add(room_id)
            log.warning("thread_auto_reopen_not_permitted")
        return True

    def _is_managed_sender(self, user_id: str) -> bool:
        return any(
            bot.client is not None and bot.client.user_id == user_id for bot in self.bots().values()
        ) or is_persisted_managed_entity_id(user_id, self.config_provider(), self.runtime_paths)

    def _joined_bots(self, room_id: str) -> list[_JoinedBot]:
        bots = self.bots()
        steward = room_steward(bots, room_id)
        return [
            (bot, client)
            for name in sorted(bots, key=lambda name: (bots[name] is not steward, name))
            if (bot := bots[name]).running and (client := bot.client) is not None and room_id in client.rooms
        ]
