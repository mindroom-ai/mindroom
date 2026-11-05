"""The listening service: each listening agent hears its rooms and runs one silent turn once a room goes quiet.

Each agent keeps a durable marker per room (``ListeningMarkers``), and each poll reads the agent's own event journal
after it. What the agent wrote, and what senders it may not hear from wrote, is passed over; the rest waits until
nothing the agent hears has been said for ``quiet_seconds``, or until the oldest of it is ten times that old, then one
silent turn hears at most ``max_messages`` of it, no more than fits its model's context window, and the marker moves
past what that turn heard. While the hourly cap holds an agent back, its rooms wait unread and keep their markers, and
they take turns at the cap, least recently run first. Each room reads the current config, and a reload while it is read
leaves its turn to the next poll, so a turn always starts under the config of its start.

No room stalls for good: a turn that keeps failing hears fewer messages until one message alone fails, which is then
skipped, and turns stay that small until they pass the first batch that failed; a message whose refetch never arrives
is skipped after a while. Each skip is logged, and a skipped message
is never heard.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import SupervisedLoop
from mindroom.constants import tracking_dir
from mindroom.contacts.turns import contact_room
from mindroom.event_journal import ConversationCursor
from mindroom.listening.markers import ListeningMarkers
from mindroom.listening.turn import heard_within_window, run_silent_turn
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import replace_visible_message
from mindroom.matrix.conversation_reads import projected_visible_messages
from mindroom.matrix.state import resolve_room_aliases

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.listening import ListenConfig
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

logger = get_logger(__name__)

_MARKERS_FILENAME = "listening_markers.json"
_POLL_SECONDS = 10
_HOUR_SECONDS = 3600
# How long a room waits on a message's refetch before it skips that message, unheard, rather than stall.
_REFETCH_WAIT_SECONDS = 600
# Failed turns in a row over the same messages before the next one hears half as many, or, at one message, skips it.
_FAILURES_PER_BATCH = 3
# How long one silent turn may run; one that runs longer is stopped and counts as failed. A poll waits on its turns, so a
# turn that hangs would otherwise stop listening everywhere.
_TURN_TIMEOUT_SECONDS = 300
# How many quiet windows the oldest heard message waits before a turn runs anyway, so a room that never pauses is heard.
_MAX_WAIT_QUIET_WINDOWS = 10
# Pages read past a turn's own to find what the agent heard last; a room with more after them counts as busy.
_QUIET_PAGES = 5


@dataclass(frozen=True)
class _Listener:
    """One agent that listens and the IDs of the rooms it listens in."""

    agent_name: str
    room_ids: tuple[str, ...]


@dataclass
class _Failures:
    """A room's silent turns that failed in a row from one marker, and how many heard messages the next one gets."""

    marker: ConversationCursor
    count: int = 0
    # None hears as many as fit.
    batch: int | None = None
    # The last message of the first batch that failed: turns keep to ``batch`` until the marker passes it.
    until: ConversationCursor | None = None


@dataclass(frozen=True)
class _Wait:
    """The message a room's reads stop at until its refetch arrives, and since when."""

    logical_event_id: str
    since: float


def _position(cursor: ConversationCursor) -> tuple[int, str]:
    return (cursor.created_ts, cursor.logical_event_id)


def _cursor(message: ResolvedVisibleMessage) -> ConversationCursor:
    """Return the position of a heard message, which a marker standing on it has heard up to."""
    return ConversationCursor(created_ts=message.timestamp, logical_event_id=message.event_id)


def _listening_in(config: Config, runtime_paths: RuntimePaths, agent_name: str, room_id: str) -> ListenConfig | None:
    """Return the agent's listening settings while it listens in the room, or None."""
    agent = config.agents.get(agent_name)
    if agent is None or agent.listen is None or room_id not in resolve_room_aliases(agent.listen.rooms, runtime_paths):
        return None
    return agent.listen


def _listeners(config: Config, runtime_paths: RuntimePaths) -> list[_Listener]:
    """Return every agent with ``listen``, which config validation allows only on shared agents."""
    return [
        _Listener(name, tuple(resolve_room_aliases(agent.listen.rooms, runtime_paths)))
        for name, agent in config.agents.items()
        if agent.listen is not None
    ]


def _first_marker(newest: ConversationCursor | None, now: float) -> ConversationCursor:
    """Return where a room is first heard from: after its newest message, and after anything sent before now.

    A message from before listening started that the journal learns of later, such as one a gap repair fetches, is
    then never heard. A newest message stamped after now is passed over, as a room's reads leave it out until then.
    """
    started = ConversationCursor(created_ts=int(now * 1000), logical_event_id="")
    if newest is None or newest.created_ts > started.created_ts:
        return started
    return max(newest, started, key=_position)


@dataclass
class ListeningService:
    """Poll every listening agent's rooms and run their silent turns, while any agent listens."""

    name = "listening"
    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config]
    bots: Callable[[], Mapping[str, AgentBot]]
    # Who may reach which agent: a silent turn hears only them, as its replies answer only them.
    agent_reply_memberships: AgentReplyMembershipIndex
    _markers: ListeningMarkers = field(init=False)
    # When each agent's recent silent turns started, for its hourly cap; a restart starts the count again.
    _runs: dict[str, list[float]] = field(default_factory=dict, init=False)
    # Rooms whose cap was reported, until a turn runs there again.
    _capped: set[tuple[str, str]] = field(default_factory=set, init=False)
    # When each room's last silent turn started; under the cap, the least recently run room goes first.
    _last_run: dict[tuple[str, str], float] = field(default_factory=dict, init=False)
    # Rooms whose turns failed in a row from their current marker.
    _failures: dict[tuple[str, str], _Failures] = field(default_factory=dict, init=False)
    # Rooms whose reads stop at a message awaiting its refetch.
    _waiting: dict[tuple[str, str], _Wait] = field(default_factory=dict, init=False)
    # Marker writes run off the event loop; a config sync's cleanup and a poll's move must not interleave.
    _marker_writes: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    # The ``(agent, room_id)`` pairs the last sync listened in; a turn that outlived its room's listening marks nothing.
    # None until the first sync, which comes before any poll.
    _listened: frozenset[tuple[str, str]] | None = field(default=None, init=False)
    # Contact rooms whose skip was reported.
    _contact_rooms: set[tuple[str, str]] = field(default_factory=set, init=False)
    # Listened rooms the agent has not joined, whose wait was reported, until it joins.
    _unjoined: set[tuple[str, str]] = field(default_factory=set, init=False)
    # Rooms whose wait for a sender's membership to resolve was reported, until it resolves.
    _membership_pending: set[tuple[str, str]] = field(default_factory=set, init=False)
    _loop: SupervisedLoop = field(init=False)

    def __post_init__(self) -> None:
        """Open the markers and create the supervised poll loop."""
        self._markers = ListeningMarkers(tracking_dir(self.runtime_paths) / _MARKERS_FILENAME)
        self._loop = SupervisedLoop("listening", self._run)

    async def sync(self, config: Config) -> None:
        """Forget the markers of rooms no agent listens in now, then run the loop while any agent listens.

        It never raises: the runtime syncs its services one after another, so a failure here would skip the rest.
        """
        try:
            listened = frozenset(
                (listener.agent_name, room_id)
                for listener in _listeners(config, self.runtime_paths)
                for room_id in listener.room_ids
            )
            async with self._marker_writes:
                self._listened = listened
                await asyncio.to_thread(self._markers.retain, listened)
        except Exception:
            logger.exception("Listening could not forget the markers of rooms it no longer listens in")
        if any(agent.listen is not None for agent in config.agents.values()):
            self.start()
        else:
            await self.stop()

    def start(self) -> None:
        """Start polling, unless it already runs."""
        self._loop.start()

    async def stop(self) -> None:
        """Stop polling; a turn it cancels kept its marker, so its messages are heard again."""
        await self._loop.stop()

    async def _run(self) -> None:
        while True:
            await self._poll_once(time.time())
            await asyncio.sleep(_POLL_SECONDS)

    async def _poll_once(self, now: float) -> None:
        """Hear every listened room each running agent has joined, and run the silent turns that are due.

        ``now`` is when the poll starts; later rooms read the clock again, as the turns before them take time.
        """
        started = time.monotonic()

        def clock() -> float:
            return now + time.monotonic() - started

        bots = self.bots()
        for listener in _listeners(self.config_provider(), self.runtime_paths):
            bot = bots.get(listener.agent_name)
            client = bot.client if bot is not None and bot.running else None
            if bot is None or client is None:
                continue
            rooms = sorted(listener.room_ids, key=lambda room_id: self._last_run.get((listener.agent_name, room_id), 0))
            for room_id in rooms:
                if not self._joined(listener.agent_name, room_id, client.rooms):
                    continue
                # One room that cannot be heard, or whose turn fails, must not stop the others.
                try:
                    await self._hear(bot, listener.agent_name, room_id, clock)
                except Exception:
                    logger.exception(
                        "Listening failed in a room; it is heard again on the next poll",
                        agent=listener.agent_name,
                        room_id=room_id,
                    )

    async def _hear(self, bot: AgentBot, agent_name: str, room_id: str, clock: Callable[[], float]) -> None:
        """Hear one room under the current config: mark where it starts, or, unless the hourly cap holds, read it."""
        config = self.config_provider()
        listen = _listening_in(config, self.runtime_paths, agent_name, room_id)
        if listen is None or self._is_contact_room(config, agent_name, room_id):
            return
        now = clock()
        marker = self._markers.get(agent_name, room_id)
        if marker is None:
            latest = await bot.conversation_reader.latest_room_cursor(room_id)
            await self._mark(agent_name, room_id, _first_marker(latest, now))
        elif not self._at_cap(agent_name, room_id, listen, now):
            await self._hear_after(bot, agent_name, room_id, listen, config, marker, clock, now)

    async def _hear_after(
        self,
        bot: AgentBot,
        agent_name: str,
        room_id: str,
        listen: ListenConfig,
        config: Config,
        marker: ConversationCursor,
        clock: Callable[[], float],
        now: float,
    ) -> None:
        """Hear one room after its marker, and run its silent turn when one is due."""
        reader = bot.conversation_reader
        # What a sync gap skipped is older than what came after it, so it is installed before the marker can pass it.
        await reader.hydrator.repair_room_history(room_id)
        # The page stops before the first message owing a refetch, and the marker waits for it, for a while. A message
        # stamped after now, which its sender's server sets, waits until then, so it cannot move the marker past others.
        page = await reader.read_room_after(room_id, marker, listen.max_messages, int(now * 1000))
        waited = self._waited(agent_name, room_id, page.stopped_at, now)
        if page.last_cursor is None and page.stopped_at is not None and waited > _REFETCH_WAIT_SECONDS:
            await self._skip_refetch(agent_name, room_id, page.stopped_at)
            return
        if page.refresh_pending:
            await reader.hydrator.resolve_refreshes(page.refresh_pending)
        if page.last_cursor is None:
            return
        try:
            heard = self._heard(bot, agent_name, room_id, projected_visible_messages(page), config)
            due = heard and await self._due(bot, agent_name, room_id, heard, page.last_cursor, listen, config, now)
        except ReplyMembershipPendingError:
            # The marker stays, so a sender whose join the router has yet to apply is heard once it has.
            if (agent_name, room_id) not in self._membership_pending:
                self._membership_pending.add((agent_name, room_id))
                logger.info("Listening waits for room membership to resolve", agent=agent_name, room_id=room_id)
            return
        self._membership_pending.discard((agent_name, room_id))
        if not heard:
            await self._mark(agent_name, room_id, page.last_cursor)
            return
        # A reload while the room was read decides again on the next poll, so the turn runs under the current config.
        if not due or self.config_provider() is not config or not self._start_run(agent_name, room_id, listen, clock()):
            return
        await self._run_turn(bot, agent_name, listen, room_id, config, marker, heard, page.last_cursor)

    async def _run_turn(
        self,
        bot: AgentBot,
        agent_name: str,
        listen: ListenConfig,
        room_id: str,
        config: Config,
        marker: ConversationCursor,
        heard: Sequence[ResolvedVisibleMessage],
        page_end: ConversationCursor,
    ) -> None:
        """Run one silent turn over the oldest of ``heard`` it can take, and move the marker past those.

        A turn that fails or runs out of time raises and keeps the marker, so the same messages are heard again.
        """
        failures = self._failures.get((agent_name, room_id))
        if failures is None or failures.marker != marker:
            failures = _Failures(marker)
        batch = heard if failures.batch is None else heard[: failures.batch]
        try:
            async with asyncio.timeout(_TURN_TIMEOUT_SECONDS):
                batch = await heard_within_window(
                    agent_name=agent_name,
                    room_id=room_id,
                    heard=batch,
                    listen=listen,
                    config=config,
                    runtime_paths=self.runtime_paths,
                )
                await run_silent_turn(
                    bot=bot,
                    agent_name=agent_name,
                    room_id=room_id,
                    heard=batch,
                    listen=listen,
                    config=config,
                    runtime_paths=self.runtime_paths,
                )
        except Exception:
            await self._turn_failed(agent_name, room_id, failures, batch)
            raise
        end = page_end if len(batch) == len(heard) else _cursor(batch[-1])
        self._failures.pop((agent_name, room_id), None)
        if failures.batch is not None and failures.until is not None and _position(end) < _position(failures.until):
            # The rest of the batch that failed is heard at the halved size too, so it is not tried whole again.
            self._failures[agent_name, room_id] = _Failures(end, batch=failures.batch, until=failures.until)
        await self._mark(agent_name, room_id, end)

    async def _turn_failed(
        self,
        agent_name: str,
        room_id: str,
        failures: _Failures,
        batch: Sequence[ResolvedVisibleMessage],
    ) -> None:
        """Count a failed turn over ``batch``; the third in a row halves the next batch, or skips a lone message."""
        failures.count += 1
        if failures.until is None:
            failures.until = _cursor(batch[-1])
        self._failures[agent_name, room_id] = failures
        if failures.count < _FAILURES_PER_BATCH:
            return
        if len(batch) > 1:
            failures.count, failures.batch = 0, len(batch) // 2
            logger.warning(
                "Listening's silent turn failed 3 times in a row; the next one hears half as many messages",
                agent=agent_name,
                room_id=room_id,
                messages=failures.batch,
            )
            return
        del self._failures[agent_name, room_id]
        logger.warning(
            "Listening skipped a message whose silent turn failed 3 times in a row; it is never heard",
            agent=agent_name,
            room_id=room_id,
            event_id=batch[0].event_id,
        )
        await self._mark(agent_name, room_id, _cursor(batch[0]))

    def _joined(self, agent_name: str, room_id: str, joined_rooms: Mapping[str, object]) -> bool:
        """Return whether the agent is in the room, reporting once that a listened room waits until it joins.

        Listening does not join rooms; the agent's ``rooms`` and invitations do.
        """
        if room_id in joined_rooms:
            self._unjoined.discard((agent_name, room_id))
            return True
        if (agent_name, room_id) not in self._unjoined:
            self._unjoined.add((agent_name, room_id))
            logger.info(
                "Listening waits until the agent joins a listened room; listening does not join rooms",
                agent=agent_name,
                room_id=room_id,
            )
        return False

    def _is_contact_room(self, config: Config, agent_name: str, room_id: str) -> bool:
        """Return whether the room is or was one of the agent's contact rooms, reporting the skip once.

        Only a reply passes the contact's message policy, so listening never acts there.
        """
        if contact_room(config, self.runtime_paths, room_id, agent_name) is None:
            return False
        if (agent_name, room_id) not in self._contact_rooms:
            self._contact_rooms.add((agent_name, room_id))
            logger.info("Listening skips a contact room", agent=agent_name, room_id=room_id)
        return True

    def _heard(
        self,
        bot: AgentBot,
        agent_name: str,
        room_id: str,
        messages: list[ResolvedVisibleMessage],
        config: Config,
    ) -> list[ResolvedVisibleMessage]:
        """Return the messages the agent hears, each shown as the one who said it.

        Like a reply, a message is judged by whom it speaks for, as ingress classifies it: the person a trusted relay
        names rather than the relaying account. The agent's own messages and senders not allowed to reach it in the
        room are left out. As for a reply, the agent's own view of the room lets a sender it has seen join wait for the
        router to apply the join, which raises ``ReplyMembershipPendingError``, rather than be left out.
        """
        observed_room = bot.client.rooms.get(room_id) if bot.client is not None else None
        heard: list[ResolvedVisibleMessage] = []
        for message in messages:
            if message.sender == bot.matrix_id.full_id:
                continue
            facts = bot.ingress_validator.classify_source(message.sender, {"content": message.content}, room_id=room_id)
            if not is_sender_allowed_for_agent_reply_in_room(
                facts.run_requester_id,
                agent_name,
                config,
                room_id,
                self.runtime_paths,
                self.agent_reply_memberships,
                require_resolved_membership=True,
                observed_room=observed_room,
            ):
                continue
            # An entity's reply written for someone stays its author's; a relayed message is its original sender's.
            speaker = facts.physical_sender if facts.acts_for_requester else facts.run_requester_id
            heard.append(replace_visible_message(message, sender=speaker))
        return heard

    async def _due(
        self,
        bot: AgentBot,
        agent_name: str,
        room_id: str,
        heard: Sequence[ResolvedVisibleMessage],
        page_end: ConversationCursor,
        listen: ListenConfig,
        config: Config,
        now: float,
    ) -> bool:
        """Return whether a silent turn over ``heard`` is due at ``now``.

        It is once nothing the agent hears has been said in the room for ``quiet_seconds``, so what it does not hear,
        such as a chatty bot or its own messages, never holds a turn back. The room's newest heard message decides, not
        the newest one on the page, so a conversation longer than ``max_messages`` still waits. A turn is due anyway
        once the oldest heard message has waited ``_MAX_WAIT_QUIET_WINDOWS`` quiet windows.
        """
        quiet_ms = listen.quiet_seconds * 1000
        now_ms = int(now * 1000)
        if heard[0].timestamp <= now_ms - _MAX_WAIT_QUIET_WINDOWS * quiet_ms:
            return True
        if heard[-1].timestamp > now_ms - quiet_ms:
            return False
        return await self._heard_nothing_after(bot, agent_name, room_id, page_end, listen, config, now_ms, quiet_ms)

    async def _heard_nothing_after(
        self,
        bot: AgentBot,
        agent_name: str,
        room_id: str,
        after: ConversationCursor,
        listen: ListenConfig,
        config: Config,
        now_ms: int,
        quiet_ms: int,
    ) -> bool:
        """Return whether the agent heard nothing said after ``after`` in the last ``quiet_ms``, reading a few pages on.

        A message awaiting its refetch ends what can be read, so its refetch is asked for and the room does not count
        as quiet until it arrives; a room with more than ``_QUIET_PAGES`` pages after ``after`` counts as not quiet.
        """
        reader = bot.conversation_reader
        for _ in range(_QUIET_PAGES):
            page = await reader.read_room_after(room_id, after, listen.max_messages, now_ms)
            if page.last_cursor is None:
                if page.refresh_pending:
                    await reader.hydrator.resolve_refreshes(page.refresh_pending)
                return page.stopped_at is None
            heard = self._heard(bot, agent_name, room_id, projected_visible_messages(page), config)
            if heard and heard[-1].timestamp > now_ms - quiet_ms:
                return False
            after = page.last_cursor
        return False

    def _at_cap(self, agent_name: str, room_id: str, listen: ListenConfig, now: float) -> bool:
        """Return whether the agent's hourly cap holds its turns back at ``now``, reporting it once per room."""
        runs = [started for started in self._runs.get(agent_name, []) if started > now - _HOUR_SECONDS]
        self._runs[agent_name] = runs
        if len(runs) < listen.max_runs_per_hour:
            return False
        if (agent_name, room_id) not in self._capped:
            self._capped.add((agent_name, room_id))
            logger.warning(
                "Listening waits: the agent reached its hourly cap; what it hears is kept for a later turn",
                agent=agent_name,
                room_id=room_id,
                max_runs_per_hour=listen.max_runs_per_hour,
            )
        return True

    def _start_run(self, agent_name: str, room_id: str, listen: ListenConfig, now: float) -> bool:
        """Count one silent turn starting at ``now``, unless the hourly cap holds it back.

        A turn that fails counts too, so a failing room retries at most ``max_runs_per_hour`` times an hour.
        """
        if self._at_cap(agent_name, room_id, listen, now):
            return False
        self._capped.discard((agent_name, room_id))
        self._last_run[agent_name, room_id] = now
        self._runs[agent_name].append(now)
        return True

    def _waited(self, agent_name: str, room_id: str, stopped_at: ConversationCursor | None, now: float) -> float:
        """Return for how many seconds the room's reads have stopped at the same message awaiting its refetch."""
        if stopped_at is None:
            self._waiting.pop((agent_name, room_id), None)
            return 0
        wait = self._waiting.get((agent_name, room_id))
        if wait is None or wait.logical_event_id != stopped_at.logical_event_id:
            wait = self._waiting[agent_name, room_id] = _Wait(stopped_at.logical_event_id, now)
        return now - wait.since

    async def _skip_refetch(self, agent_name: str, room_id: str, stopped_at: ConversationCursor) -> None:
        """Move the marker past the message the room's reads stop at, whose refetch never arrived."""
        self._waiting.pop((agent_name, room_id), None)
        logger.warning(
            "Listening skipped a message whose refetch did not arrive in 10 minutes; it is never heard",
            agent=agent_name,
            room_id=room_id,
            event_id=stopped_at.logical_event_id,
        )
        await self._mark(agent_name, room_id, stopped_at)

    async def _mark(self, agent_name: str, room_id: str, cursor: ConversationCursor) -> None:
        async with self._marker_writes:
            # A sync that stopped listening in the room while its turn ran forgot the marker, and it stays forgotten.
            if self._listened is not None and (agent_name, room_id) not in self._listened:
                return
            await asyncio.to_thread(self._markers.set, agent_name, room_id, cursor)
