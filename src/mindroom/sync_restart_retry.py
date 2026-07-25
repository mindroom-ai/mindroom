"""One-shot re-dispatch of responses cancelled by sync-restart recovery.

When the Matrix sync watchdog restarts a stalled sync loop, in-flight
responses are cancelled and their placeholder becomes a terminal
"[Response interrupted by service restart]" note. The turn controller
registers a retry here, and the bot flushes the queue once its sync loop
reports a healthy sync response again. Each source event is retried at
most once; a retry that is itself interrupted is not requeued. A retry
refused by a config apply is the exception: nothing ran, so it stays
pending for the next flush. Pending room ids also let the orchestrator
hand retries to a replacement bot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SOURCE_EVENT_IDS_METADATA_KEY
from mindroom.history.storage import is_model_history_visible_run
from mindroom.logging_config import get_logger
from mindroom.response_admission import ResponseAdmissionRefusedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.history.types import HistoryScope

logger = get_logger(__name__)

_MAX_ATTEMPTED_KEYS = 512
_INTERRUPTED_REPLAY_STATE_KEY = "mindroom_replay_state"
_INTERRUPTED_REPLAY_STATE = "interrupted"


@dataclass(frozen=True)
class _PendingRetry:
    """One retry callback plus the Matrix room containing its terminal marker."""

    room_id: str
    callback: Callable[[], Awaitable[None]]


def _run_matches_scope(run: RunOutput | TeamRunOutput, scope: HistoryScope) -> bool:
    """Return whether one stored run belongs to the requested history scope."""
    if scope.kind == "team":
        return isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id
    return isinstance(run, RunOutput) and run.agent_id == scope.scope_id


def _run_source_event_ids(run: RunOutput | TeamRunOutput) -> set[str] | None:
    """Return valid source event IDs, or None when provenance is absent or malformed."""
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
        if not is_model_history_visible_run(run) or not _run_matches_scope(run, scope):
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

    _pending: dict[str, _PendingRetry] = field(default_factory=dict)
    _attempted: dict[str, None] = field(default_factory=dict)
    _flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def has_pending(self) -> bool:
        """Return whether any retry is waiting for sync recovery."""
        return bool(self._pending)

    @property
    def pending_room_ids(self) -> frozenset[str]:
        """Return rooms whose interrupted turns still await a retry."""
        return frozenset(pending.room_id for pending in self._pending.values())

    def register(self, key: str, retry: Callable[[], Awaitable[None]], *, room_id: str) -> bool:
        """Queue one retry for a source event; refuse anything already seen."""
        if key in self._attempted or key in self._pending:
            return False
        self._pending[key] = _PendingRetry(room_id=room_id, callback=retry)
        logger.info("sync_restart_retry_queued", source_event_id=key, pending_count=len(self._pending))
        return True

    def _mark_attempted(self, key: str) -> None:
        """Record one attempted key, bounding the dedup memory."""
        self._attempted[key] = None
        while len(self._attempted) > _MAX_ATTEMPTED_KEYS:
            self._attempted.pop(next(iter(self._attempted)))

    async def flush(self) -> None:
        """Run every queued retry exactly once in FIFO order, isolating individual failures."""
        async with self._flush_lock:
            while self._pending:
                key = next(iter(self._pending))
                pending = self._pending[key]
                retry = pending.callback
                logger.info("sync_restart_retry_started", source_event_id=key)
                # Burn the key unless admission refused it. Promoting only after the
                # retry actually ran means there is no rollback to get wrong:
                # _mark_attempted evicts the oldest key once it hits its bound, and
                # that eviction cannot be undone.
                attempted = True
                try:
                    await retry()
                except ResponseAdmissionRefusedError:
                    # A config apply refused the response before any lifecycle work,
                    # so nothing ran. Stop the pass rather than re-raising: the gate
                    # stays closed for the whole apply, so every pending retry must
                    # stay queued for the next flush.
                    attempted = False
                    logger.info("sync_restart_retry_deferred_by_config_apply", source_event_id=key)
                    return
                except asyncio.CancelledError:
                    # The flush task is being torn down mid-retry; the retry did run,
                    # so log the dead end before propagating.
                    logger.warning("sync_restart_retry_cancelled", source_event_id=key)
                    raise
                except Exception:
                    logger.exception("sync_restart_retry_failed", source_event_id=key)
                finally:
                    if attempted:
                        self._pending.pop(key)
                        self._mark_attempted(key)
