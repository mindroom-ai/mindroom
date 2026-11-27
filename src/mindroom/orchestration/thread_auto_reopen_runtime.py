"""Orchestrator runtime coordinator that automatically reopens resolved threads on new replies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from mindroom.logging_config import get_logger
from mindroom.requester_identity import is_persisted_managed_entity_id
from mindroom.room_steward import room_steward
from mindroom.thread_auto_reopen import (
    ResolvedThread,
    event_origin_server_ts,
    extract_auto_reopen_reply,
    resolved_from_state_event,
)
from mindroom.thread_tags import (
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagRecord,
    ThreadTagsPermissionError,
    reopen_resolved_thread_record,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    import nio

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_MAX_PENDING_ROOMS = 256
_MAX_PENDING_THREADS_PER_ROOM = 256
type _ThreadKey = tuple[str, str]
type _BotWriteOutcome = Literal["done", "denied", "transient"]


@dataclass
class ThreadAutoReopenCoordinator:
    """Track resolved threads in memory and clear ``resolved`` when a newer reply arrives."""

    name = "thread_auto_reopen"
    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config]
    bots: Callable[[], Mapping[str, AgentBot]]
    retry_delays: tuple[float, ...] = (1.0, 3.0)
    _resolved: dict[str, dict[str, ResolvedThread]] = field(default_factory=dict, init=False)
    _restored_rooms: set[str] = field(default_factory=set, init=False)
    _live_during_restore: dict[str, set[str]] = field(default_factory=dict, init=False)
    _pending_before_restore: dict[str, dict[str, int]] = field(default_factory=dict, init=False)
    _in_flight: dict[_ThreadKey, tuple[ThreadTagRecord, asyncio.Task[None]]] = field(default_factory=dict, init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _permission_warned_rooms: set[str] = field(default_factory=set, init=False)
    _stopped: bool = field(default=False, init=False)

    async def sync(self, config: Config) -> None:  # noqa: ARG002 - auto-reopen reads live config on demand
        """Mark the coordinator active."""
        self._stopped = False

    async def stop(self) -> None:
        """Stop scheduling reopens and cancel all in-flight background reopen tasks."""
        self._stopped = True
        tasks = list(self._tasks)
        self._tasks.clear()
        self._in_flight.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def begin_restore(self, room_id: str) -> None:
        """Mark a room restore in progress so live tag events take precedence over the snapshot."""
        self._restored_rooms.discard(room_id)
        self._live_during_restore[room_id] = set()
        self._permission_warned_rooms.discard(room_id)

    def restore_room_events(self, room_id: str, events: Sequence[Mapping[str, object]]) -> None:
        """Seed resolved threads from an already-fetched room state snapshot and drain buffered replies."""
        live_roots = self._live_during_restore.get(room_id, set())
        if (room_resolved := self._resolved.get(room_id)) is not None:
            for root_id in list(room_resolved):
                if root_id not in live_roots:
                    self._drop_entry(room_id, root_id)
        for event in events:
            parsed = resolved_from_state_event(room_id, event)
            if parsed is not None and parsed[0] not in live_roots:
                self._apply_entry(room_id, parsed[0], parsed[1], tombstone_ts=event_origin_server_ts(event))
        self._restored_rooms.add(room_id)
        pending = self._pending_before_restore.pop(room_id, None)
        if pending and not self._stopped:
            for thread_root_id, max_ts in pending.items():
                self._maybe_schedule_reopen(room_id, thread_root_id, max_ts)

    def end_restore(self, room_id: str) -> None:
        """Finish tracking live state events for a room restore attempt."""
        self._live_during_restore.pop(room_id, None)

    def forget_room(self, room_id: str) -> None:
        """Clear cached state for a room that no running bot is watching anymore."""
        self._restored_rooms.discard(room_id)
        self._resolved.pop(room_id, None)
        self._pending_before_restore.pop(room_id, None)
        self._permission_warned_rooms.discard(room_id)
        for key, (_record, task) in list(self._in_flight.items()):
            if key[0] == room_id and task is not asyncio.current_task() and not task.done():
                task.cancel()

    def room_left(self, room_id: str) -> None:
        """Forget a room once its last running bot has left it."""
        if next(self._joined_bots(room_id), None) is None:
            self.forget_room(room_id)

    def observe_power_levels_changed(self, room_id: str) -> None:
        """Allow permission warnings again after a room's power levels change."""
        self._permission_warned_rooms.discard(room_id)

    def resolved(self, room_id: str, thread_root_id: str, record: ThreadTagRecord) -> None:
        """Provisionally index a ``resolved`` tag this process just wrote before its sync echo arrives."""
        entry = ResolvedThread(room_id, thread_root_id, int(record.set_at.timestamp() * 1000), record)
        self._mark_live(room_id, thread_root_id)
        self._apply_entry(room_id, thread_root_id, entry, tombstone_ts=None)

    def reopened(self, room_id: str, thread_root_id: str) -> None:
        """Drop a ``resolved`` tag this process just removed."""
        self._mark_live(room_id, thread_root_id)
        self._drop_entry(room_id, thread_root_id)

    def observe_state_event(self, room_id: str, source: Mapping[str, object]) -> None:
        """Index or drop a thread's ``resolved`` state from a live Matrix state event."""
        parsed = resolved_from_state_event(room_id, source)
        if parsed is None:
            return
        thread_root_id, entry = parsed
        self._mark_live(room_id, thread_root_id)
        self._apply_entry(room_id, thread_root_id, entry, tombstone_ts=event_origin_server_ts(source))

    def observe_timeline_event(self, room_id: str, source: Mapping[str, object]) -> None:
        """Inspect one newly admitted non-history cleartext timeline event and schedule reopen if needed."""
        if self._stopped:
            return
        restored = room_id in self._restored_rooms
        if restored and room_id not in self._resolved:
            return
        reply = extract_auto_reopen_reply(source, sender_is_managed=self._is_managed_sender)
        if reply is None:
            return
        thread_root_id, _event_id, origin_server_ts = reply
        if not restored:
            self._buffer_pending(room_id, thread_root_id, origin_server_ts)
            return
        self._maybe_schedule_reopen(room_id, thread_root_id, origin_server_ts)

    def _mark_live(self, room_id: str, thread_root_id: str) -> None:
        if (live_roots := self._live_during_restore.get(room_id)) is not None:
            live_roots.add(thread_root_id)

    def _apply_entry(
        self,
        room_id: str,
        thread_root_id: str,
        entry: ResolvedThread | None,
        *,
        tombstone_ts: int | None,
    ) -> None:
        existing = self._resolved.get(room_id, {}).get(thread_root_id)
        if entry is None:
            if existing is not None and (tombstone_ts is None or tombstone_ts >= existing.resolved_ts):
                self._drop_entry(room_id, thread_root_id)
            return
        if existing is not None:
            if entry.state_event_id is not None and existing.state_event_id == entry.state_event_id:
                return
            provisional_echo = (
                existing.state_event_id is None and entry.state_event_id is not None and entry.record == existing.record
            )
            if not provisional_echo and entry.resolved_ts < existing.resolved_ts:
                return
        self._resolved.setdefault(room_id, {})[thread_root_id] = entry

    def _drop_entry(self, room_id: str, thread_root_id: str) -> None:
        if (room_resolved := self._resolved.get(room_id)) is None:
            return
        room_resolved.pop(thread_root_id, None)
        if not room_resolved:
            del self._resolved[room_id]
        if (pending := self._in_flight.get((room_id, thread_root_id))) is not None:
            task = pending[1]
            if task is not asyncio.current_task() and not task.done():
                task.cancel()

    def _buffer_pending(self, room_id: str, thread_root_id: str, origin_server_ts: int) -> None:
        pending = self._pending_before_restore.get(room_id)
        if pending is None:
            if len(self._pending_before_restore) >= _MAX_PENDING_ROOMS:
                del self._pending_before_restore[next(iter(self._pending_before_restore))]
            pending = self._pending_before_restore[room_id] = {}
        if thread_root_id in pending:
            pending[thread_root_id] = max(pending[thread_root_id], origin_server_ts)
            return
        if len(pending) >= _MAX_PENDING_THREADS_PER_ROOM:
            del pending[next(iter(pending))]
        pending[thread_root_id] = origin_server_ts

    def _maybe_schedule_reopen(self, room_id: str, thread_root_id: str, origin_server_ts: int) -> None:
        entry = self._resolved.get(room_id, {}).get(thread_root_id)
        if entry is None or origin_server_ts <= entry.resolved_ts or self._stopped:
            return
        key = (room_id, thread_root_id)
        if (existing := self._in_flight.get(key)) is not None:
            in_flight_record, in_flight_task = existing
            if in_flight_record == entry.record and not in_flight_task.done():
                return
            if not in_flight_task.done():
                in_flight_task.cancel()
        task = asyncio.create_task(self._reopen_thread(entry), name=f"thread_auto_reopen:{room_id}:{thread_root_id}")
        self._in_flight[key] = (entry.record, task)
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._finish_task(key, entry.record, done))

    def _finish_task(self, key: _ThreadKey, record: ThreadTagRecord, done: asyncio.Task[None]) -> None:
        self._tasks.discard(done)
        current = self._in_flight.get(key)
        if current is not None and current[0] == record and current[1] is done:
            del self._in_flight[key]

    def _is_current(self, entry: ResolvedThread) -> bool:
        current = self._resolved.get(entry.room_id, {}).get(entry.thread_root_id)
        return current is not None and current.record == entry.record

    async def _reopen_thread(self, entry: ResolvedThread) -> None:
        attempts = (0.0, *self.retry_delays)
        for attempt_index, delay in enumerate(attempts):
            if delay > 0:
                await asyncio.sleep(delay)
            if self._stopped or not self._is_current(entry):
                return
            if await self._attempt_reopen(entry):
                return
            if attempt_index == len(attempts) - 1:
                logger.warning(
                    "thread_auto_reopen_exhausted",
                    room_id=entry.room_id,
                    thread_root_id=entry.thread_root_id,
                    attempts=len(attempts),
                )

    async def _attempt_reopen(self, entry: ResolvedThread) -> bool:
        """Try joined bots in steward-first order; return True when finished and False to retry."""
        joined = list(self._joined_bots(entry.room_id))
        if not joined:
            self.forget_room(entry.room_id)
            return True
        saw_transient_error = False
        for bot, client in joined:
            outcome = await self._reopen_with_bot(bot, client, entry)
            if outcome == "done":
                return True
            if outcome == "transient":
                saw_transient_error = True
        if saw_transient_error:
            return False
        if entry.room_id not in self._permission_warned_rooms:
            self._permission_warned_rooms.add(entry.room_id)
            logger.warning(
                "thread_auto_reopen_not_permitted",
                room_id=entry.room_id,
                thread_root_id=entry.thread_root_id,
            )
        return True

    async def _reopen_with_bot(
        self,
        bot: AgentBot,
        client: nio.AsyncClient,
        entry: ResolvedThread,
    ) -> _BotWriteOutcome:
        room = client.rooms.get(entry.room_id)
        if room is not None and not room.power_levels.can_user_send_state(client.user_id, THREAD_TAGS_EVENT_TYPE):
            return "denied"
        try:
            cleared = await reopen_resolved_thread_record(
                client,
                entry.room_id,
                entry.thread_root_id,
                expected_record=entry.record,
                requester_user_id=None,
            )
        except ThreadTagsPermissionError:
            logger.info("thread_auto_reopen_clear_denied", agent=bot.agent_name, room_id=entry.room_id)
            return "denied"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "thread_auto_reopen_clear_failed",
                agent=bot.agent_name,
                room_id=entry.room_id,
                thread_root_id=entry.thread_root_id,
                error=str(exc),
            )
            return "transient"
        self._permission_warned_rooms.discard(entry.room_id)
        if cleared and self._is_current(entry):
            self._drop_entry(entry.room_id, entry.thread_root_id)
        logger.info(
            "thread_auto_reopened" if cleared else "thread_auto_reopen_changed_before_clear",
            agent=bot.agent_name,
            room_id=entry.room_id,
            thread_root_id=entry.thread_root_id,
        )
        return "done"

    def _is_managed_sender(self, user_id: str) -> bool:
        return any(
            bot.client is not None and bot.client.user_id == user_id for bot in self.bots().values()
        ) or is_persisted_managed_entity_id(user_id, self.config_provider(), self.runtime_paths)

    def _joined_bots(self, room_id: str) -> Iterator[tuple[AgentBot, nio.AsyncClient]]:
        """Yield running bots joined to ``room_id`` with the steward first and the rest by name."""
        bots = self.bots()
        steward = room_steward(bots, room_id)
        for name in sorted(bots, key=lambda name: (bots[name] is not steward, name)):
            bot = bots[name]
            client = bot.client
            if bot.running and client is not None and room_id in client.rooms:
                yield bot, client
