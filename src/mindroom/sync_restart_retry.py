"""Rooms whose turns were interrupted by a sync-restart shutdown.

Only a bot being replaced cancels responses with sync-restart provenance: an
automatic receive-loop restart leaves live responses with their original owner.
The interrupted response's placeholder becomes a terminal "[Response interrupted
by service restart]" note, so the turn controller records its room here and the
orchestrator hands those rooms to the replacement bot, whose stale-stream
recovery re-drives the interrupted turns. Each source event is recorded once, so
one interrupted turn cannot claim two recovery attempts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SOURCE_EVENT_IDS_METADATA_KEY
from mindroom.history.storage import is_model_history_visible_run
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.history.types import HistoryScope

logger = get_logger(__name__)

_INTERRUPTED_REPLAY_STATE_KEY = "mindroom_replay_state"
_INTERRUPTED_REPLAY_STATE = "interrupted"


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
class InterruptedTurnRooms:
    """Hold the rooms of turns interrupted by a sync-restart shutdown."""

    _pending: dict[str, str] = field(default_factory=dict)

    @property
    def pending_room_ids(self) -> frozenset[str]:
        """Return rooms whose interrupted turns still await replacement recovery."""
        return frozenset(self._pending.values())

    def register(self, key: str, *, room_id: str) -> bool:
        """Record one interrupted source event; refuse anything already seen."""
        if key in self._pending:
            return False
        self._pending[key] = room_id
        logger.info("sync_restart_interrupted_turn_recorded", source_event_id=key, pending_count=len(self._pending))
        return True
