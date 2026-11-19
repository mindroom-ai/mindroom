"""Call transcripts in the runtime: the service, the bots it reads and posts through, its loop, and agent calls."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

from mindroom.background_tasks import SupervisedLoop
from mindroom.call_transcripts.call_room import CallRoom
from mindroom.call_transcripts.notes import ModelNotesWriter
from mindroom.call_transcripts.service import AgentCallRoom, CallTranscriptService
from mindroom.call_transcripts.store import CallTranscriptStore, database_path
from mindroom.call_transcripts.transcriber import SpeechTranscriber
from mindroom.logging_config import get_logger
from mindroom.room_steward import room_poster, room_steward

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import nio

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# How long the loop waits while call transcripts are off, and after a pass that failed.
_IDLE_SECONDS = 15.0


@dataclass
class CallTranscriptsRuntimeCoordinator:
    """Own the call transcript service, lend it each room's steward and poster, and run the loop checking its calls.

    Also find the agent call to read live in a room, through the agent answering calls there.
    """

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config]
    bots: Callable[[], Mapping[str, AgentBot]]
    """The runtime's current bots, whose agents' calls are read live."""
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.time
    """Wall-clock time, which calls are kept by."""
    _bound: bool = field(default=False, init=False)
    _service: CallTranscriptService | None = field(default=None, init=False)
    _agent_rooms: dict[str, CallRoom] = field(default_factory=dict, init=False)
    """Each agent's view of the rooms it answers calls in, kept so polls share its reads of a room."""
    _loop: SupervisedLoop = field(init=False)

    def __post_init__(self) -> None:
        """Create the loop, which starts once the runtime first binds its bots, whether call transcripts are on or off."""
        self._loop = SupervisedLoop("call_transcripts", self._run)

    def bind_if_ready(self, config: Config, bots: Mapping[str, AgentBot]) -> None:
        """Start the loop, and read and post through the running bots while call transcripts are on."""
        self._loop.start()
        if not config.calls.transcripts.enabled and self._service is not None:
            # Writes under way stop now, and none is sent while call transcripts are off.
            self._service.cancel_writes()
        self._bound = config.calls.transcripts.enabled and any(bot.running for bot in bots.values())

    def unbind(self) -> None:
        """Stop reading and posting through the bots, before any stops or is replaced; calls wait for the next bind."""
        self._bound = False

    async def stop(self) -> None:
        """Unbind the bots, stop the loop and the service, cancelling a pass and the messages being posted.

        No row or message they sent is posted twice.
        """
        self.unbind()
        await self._loop.stop()
        if self._service is not None:
            await self._service.stop()

    def service(self) -> CallTranscriptService | None:
        """Return the service while call transcripts are on, the same one each time; None while they are off."""
        if not self.config_provider().calls.transcripts.enabled:
            return None
        return self._made_service()

    def agent_call_room(self, room_id: str) -> AgentCallRoom | None:
        """Return the room as the agent answering calls in it reads it, with that agent's call there while it is in one.

        None when no agent answers calls in the room. This holds whether call transcripts are on or off.
        """
        watching: str | None = None
        for agent_name, bot in self.bots().items():
            manager = bot.call_manager
            if manager is None:
                continue
            if (call := manager.live_call(room_id)) is not None:
                return AgentCallRoom(room_id=room_id, room=self._agent_room(agent_name), call=call)
            if watching is None and manager.watches(room_id):
                watching = agent_name
        return None if watching is None else AgentCallRoom(room_id=room_id, room=self._agent_room(watching), call=None)

    def _agent_room(self, agent_name: str) -> CallRoom:
        room = self._agent_rooms.get(agent_name)
        if room is None:
            # Only who is in the room and their names are read through it; an agent's view never posts or retitles.
            room = CallRoom(
                ready=lambda: self._agent_client(agent_name) is not None,
                reader=partial(self._agent_client, agent_name),
                poster=lambda _room_id: None,
                retitle=lambda *_: None,
            )
            self._agent_rooms[agent_name] = room
        return room

    def _agent_client(self, agent_name: str, room_id: str | None = None) -> nio.AsyncClient | None:
        """Return the agent's client once its bot runs and has synced its rooms, for a room only if it is in it."""
        bot = self.bots().get(agent_name)
        if bot is None or not bot.running or not bot.first_sync_complete or bot.client is None:
            return None
        return bot.client if room_id is None or room_id in bot.client.rooms else None

    def _made_service(self) -> CallTranscriptService:
        if self._service is None:
            self._service = CallTranscriptService(
                CallTranscriptStore(self.runtime_paths.storage_root),
                CallRoom(
                    ready=self._ready,
                    reader=lambda room_id: _client(room_steward(self.bots(), room_id)),
                    poster=lambda room_id: _client(room_poster(self.bots(), room_id)),
                    retitle=self._retitle,
                ),
                SpeechTranscriber(self.config_provider, self.runtime_paths),
                ModelNotesWriter(self.config_provider, self.runtime_paths),
                clock=self.clock,
            )
        return self._service

    def _ready(self) -> bool:
        """Return whether rooms can be read and posted to: bound, call transcripts on, every running bot synced.

        A bot that has not synced does not know its rooms yet, and a room it alone is in would read as having no agent.
        """
        running = [bot for bot in self.bots().values() if bot.running]
        return (
            self._bound
            and self.config_provider().calls.transcripts.enabled
            and bool(running)
            and all(bot.first_sync_complete for bot in running)
        )

    def _retitle(self, room_id: str, thread_id: str, event_id: str, body: str) -> None:
        """Have the room's poster summarize the thread now, whatever its count, counting the message it just posted."""
        poster = room_poster(self.bots(), room_id) if self._ready() else None
        if poster is not None:
            poster.post_response_effects.retitle_thread(room_id, thread_id, event_id=event_id, body=body)

    async def _run(self) -> None:
        while True:
            await self.sleep(await self._pass())

    async def _pass(self) -> float:
        """Check every call, and return how long to wait before the next pass.

        While call transcripts are off, a pass holds the store to the one-day limit, touching no Matrix, and makes no
        call database where there is none.
        """
        try:
            if self.config_provider().calls.transcripts.enabled:
                return await self._made_service().tick()
            if self._service is not None or database_path(self.runtime_paths.storage_root).exists():
                await self._made_service().tick_while_off()
        except Exception:
            logger.exception("call_transcripts_pass_failed")
        return _IDLE_SECONDS


def _client(bot: AgentBot | None) -> nio.AsyncClient | None:
    return None if bot is None else bot.client
