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

from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SOURCE_EVENT_IDS_METADATA_KEY
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.history.types import HistoryScope

logger = get_logger(__name__)

_MAX_ATTEMPTED_KEYS = 512
_INTERRUPTED_REPLAY_STATE_KEY = "mindroom_replay_state"
_INTERRUPTED_REPLAY_STATE = "interrupted"
_MODEL_INVISIBLE_RUN_STATUSES = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}


def _run_matches_scope(run: RunOutput | TeamRunOutput, scope: HistoryScope) -> bool:
    if scope.kind == "team":
        return isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id
    return isinstance(run, RunOutput) and run.agent_id == scope.scope_id


def _run_source_event_ids(run: RunOutput | TeamRunOutput) -> set[str] | None:
    metadata = run.metadata
    if not isinstance(metadata, dict):
        return None
    source_event_id = metadata.get(MATRIX_EVENT_ID_METADATA_KEY)
    source_event_ids = metadata.get(MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)
    if source_event_id is not None and (not isinstance(source_event_id, str) or not source_event_id):
        return None
    if source_event_ids is not None and (
        not isinstance(source_event_ids, list)
        or any(not isinstance(value, str) or not value for value in source_event_ids)
    ):
        return None
    event_ids = [source_event_id, *(source_event_ids or ())]
    return {event_id for event_id in event_ids if event_id} or None


def interrupted_source_needs_retry(
    runs: Sequence[RunOutput | TeamRunOutput],
    *,
    scope: HistoryScope,
    source_event_id: str,
) -> bool:
    """Return whether stored run order ends in this source's interrupted replay."""
    interrupted_replay_found = False
    for run in runs:
        if (
            run.parent_run_id is not None
            or run.status in _MODEL_INVISIBLE_RUN_STATUSES
            or not _run_matches_scope(run, scope)
        ):
            continue
        run_source_event_ids = _run_source_event_ids(run)
        if run_source_event_ids is None:
            if interrupted_replay_found:
                return False
            continue
        if source_event_id not in run_source_event_ids:
            continue
        if interrupted_replay_found:
            return False
        metadata = run.metadata
        assert isinstance(metadata, dict)
        interrupted_replay_found = metadata.get(_INTERRUPTED_REPLAY_STATE_KEY) == _INTERRUPTED_REPLAY_STATE
    return interrupted_replay_found


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
