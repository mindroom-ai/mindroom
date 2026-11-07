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
from mindroom.call_transcripts.store import CallTranscriptStore
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
        """Create the loop, which starts once the router is first bound."""
        self._loop = SupervisedLoop("call_transcripts", self._run)

    def bind_if_ready(self, config: Config, bots: Mapping[str, AgentBot]) -> None:
        """Read and post through the router while it runs and call transcripts are on, and start the loop."""
        router = bots.get(ROUTER_AGENT_NAME)
        if not config.calls.transcripts.enabled or router is None or router.client is None or not router.running:
            self.unbind()
            return
        self._router = router
        self._loop.start()

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
        """Return the bound router's client once it runs and has synced its rooms."""
        router = self._router
        if router is None or not router.running or not router.first_sync_complete:
            return None
        return router.client

    async def _run(self) -> None:
        while True:
            await self.sleep(await self._pass())

    async def _pass(self) -> float:
        """Check every call while call transcripts are on, and return how long to wait before the next pass."""
        service = self.service()
        if service is None:
            return _IDLE_SECONDS
        try:
            return await service.tick()
        except Exception:
            logger.exception("call_transcripts_pass_failed")
            return _IDLE_SECONDS
