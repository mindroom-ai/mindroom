"""Retry obligation settlement after terminal turn persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task, run_blocking_until_complete
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.dispatch_obligations import DispatchObligationStore

logger = get_logger(__name__)

_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_MAX_DELAY_SECONDS = 30.0


@dataclass
class TurnSettlementRetry:
    """Land dispatch-obligation settlement after TurnStore persistence."""

    store: DispatchObligationStore
    background_task_owner: object | None = None
    _retry_initial_delay_seconds: float = field(default=_RETRY_INITIAL_DELAY_SECONDS, repr=False)
    _retry_max_delay_seconds: float = field(default=_RETRY_MAX_DELAY_SECONDS, repr=False)
    _event_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _source_event_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def bind_event_loop(self) -> None:
        """Bind the active runtime loop for callbacks arriving from persistence workers."""
        self._event_loop = asyncio.get_running_loop()

    def retry(self, source_event_ids: tuple[str, ...]) -> None:
        """Settle terminal sources now or retry them on the active runtime loop."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is None:
            try:
                self.store.settle_pending_from_turn_store(source_event_ids)
            except Exception:
                logger.exception(
                    "turn_dispatch_obligation_initial_settlement_failed",
                    source_event_ids=source_event_ids,
                )
            else:
                return
            event_loop = self._event_loop
            if event_loop is None or event_loop.is_closed():
                logger.error(
                    "turn_dispatch_obligation_retry_loop_unavailable",
                    source_event_ids=source_event_ids,
                )
                return
            event_loop.call_soon_threadsafe(self._enqueue, source_event_ids)
            return
        self._event_loop = running_loop
        self._enqueue(source_event_ids)

    def _enqueue(self, source_event_ids: tuple[str, ...]) -> None:
        """Add exact terminal sources to the loop-owned settlement retry set."""
        self._source_event_ids.update(source_event_ids)
        if self._task is not None and not self._task.done():
            return
        self._task = create_background_task(
            self._run(),
            name=f"retry_turn_dispatch_settlement_{self.store.entity_name}",
            owner=self.background_task_owner,
        )

    async def _run(self) -> None:
        """Retry terminal obligation settlement with capped backoff until it lands."""
        retry_delay_seconds = self._retry_initial_delay_seconds
        try:
            while self._source_event_ids:
                source_event_ids = tuple(self._source_event_ids)
                try:
                    await run_blocking_until_complete(
                        self.store.settle_pending_from_turn_store,
                        source_event_ids,
                    )
                except asyncio.CancelledError as error:
                    logger.info(
                        "turn_dispatch_obligation_settlement_retry_cancelled",
                        source_event_ids=tuple(self._source_event_ids),
                        cancellation_message=str(error) or None,
                    )
                    raise
                except Exception:
                    logger.exception(
                        "turn_dispatch_obligation_settlement_retry_failed",
                        source_event_ids=source_event_ids,
                    )
                    await asyncio.sleep(retry_delay_seconds)
                    retry_delay_seconds = min(
                        retry_delay_seconds * 2,
                        self._retry_max_delay_seconds,
                    )
                else:
                    self._source_event_ids.difference_update(source_event_ids)
                    retry_delay_seconds = self._retry_initial_delay_seconds
        finally:
            self._task = None
