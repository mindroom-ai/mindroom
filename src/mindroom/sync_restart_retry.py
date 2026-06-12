"""One-shot re-dispatch of responses cancelled by sync-restart recovery.

When the Matrix sync watchdog restarts a stalled sync loop, in-flight
responses are cancelled and their placeholder becomes a terminal
"[Response interrupted by service restart]" note. The turn controller
registers a retry here, and the bot flushes the queue once its sync loop
reports a healthy sync response again. Each source event is retried at
most once; a retry that is itself interrupted is not requeued.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = get_logger(__name__)

_MAX_ATTEMPTED_KEYS = 512


@dataclass
class SyncRestartRetryQueue:
    """Hold one-shot retry callbacks keyed by source event id."""

    _pending: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    _attempted: dict[str, None] = field(default_factory=dict)

    @property
    def has_pending(self) -> bool:
        """Return whether any retry is waiting for sync recovery."""
        return bool(self._pending)

    def register(self, key: str, retry: Callable[[], Awaitable[None]]) -> bool:
        """Queue one retry for a source event; refuse anything already seen."""
        if key in self._attempted or key in self._pending:
            return False
        self._pending[key] = retry
        logger.info("sync_restart_retry_queued", source_event_id=key, pending_count=len(self._pending))
        return True

    def _mark_attempted(self, key: str) -> None:
        """Record one attempted key, bounding the dedup memory."""
        self._attempted[key] = None
        while len(self._attempted) > _MAX_ATTEMPTED_KEYS:
            self._attempted.pop(next(iter(self._attempted)))

    async def flush(self) -> None:
        """Run every queued retry exactly once in FIFO order, isolating individual failures."""
        while self._pending:
            key = next(iter(self._pending))
            retry = self._pending.pop(key)
            self._mark_attempted(key)
            logger.info("sync_restart_retry_started", source_event_id=key)
            try:
                await retry()
            except asyncio.CancelledError:
                # The flush task is being torn down mid-retry; the key was already
                # promoted to attempted, so log the dead end before propagating.
                logger.warning("sync_restart_retry_cancelled", source_event_id=key)
                raise
            except Exception:
                logger.exception("sync_restart_retry_failed", source_event_id=key)
