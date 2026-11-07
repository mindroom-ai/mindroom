"""Transcripts of calls between people in the runtime: the service, the router it reads and posts through, its loop."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.background_tasks import SupervisedLoop
from mindroom.call_transcripts.notes import ModelNotesWriter
from mindroom.call_transcripts.router_room import RouterCallRoom
from mindroom.call_transcripts.service import CallTranscriptService
from mindroom.call_transcripts.store import CallTranscriptStore, database_path
from mindroom.call_transcripts.transcriber import SpeechTranscriber
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

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
    """Own the call transcript service, the router it reads rooms and posts through, and the loop checking its calls."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.time
    """Wall-clock time, which calls are kept by."""
    _router: AgentBot | None = field(default=None, init=False)
    _service: CallTranscriptService | None = field(default=None, init=False)
    _loop: SupervisedLoop = field(init=False)

    def __post_init__(self) -> None:
        """Create the loop, which starts once the runtime first binds its routers, whether call transcripts are on or off."""
        self._loop = SupervisedLoop("call_transcripts", self._run)

    def bind_if_ready(self, config: Config, bots: Mapping[str, AgentBot]) -> None:
        """Start the loop, and read and post through the router while it runs and call transcripts are on."""
        self._loop.start()
        if not config.calls.transcripts.enabled and self._service is not None:
            # Writes under way stop now, and none is sent while call transcripts are off.
            self._service.cancel_writes()
        router = bots.get(ROUTER_AGENT_NAME)
        if not config.calls.transcripts.enabled or router is None or router.client is None or not router.running:
            self.unbind()
            return
        self._router = router

    def unbind(self) -> None:
        """Stop reading and posting through the router, before it stops or is replaced; calls wait for the next one."""
        self._router = None

    async def stop(self) -> None:
        """Unbind the router, stop the loop and the service, cancelling a pass and the messages being posted.

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

    def _made_service(self) -> CallTranscriptService:
        if self._service is None:
            self._service = CallTranscriptService(
                CallTranscriptStore(self.runtime_paths.storage_root),
                RouterCallRoom(client=self._router_client),
                SpeechTranscriber(self.config_provider, self.runtime_paths),
                ModelNotesWriter(self.config_provider, self.runtime_paths),
                clock=self.clock,
            )
        return self._service

    def _router_client(self) -> nio.AsyncClient | None:
        """Return the bound router's client once it runs and has synced its rooms, while call transcripts are on."""
        router = self._router
        if router is None or not router.running or not router.first_sync_complete:
            return None
        return router.client if self.config_provider().calls.transcripts.enabled else None

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
