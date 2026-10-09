"""Decide whether an edit regeneration of an already committed revision may run again.

The retry is current only while persisted history still ends in that source's
interrupted replay record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SOURCE_EVENT_IDS_METADATA_KEY
from mindroom.history_run_visibility import is_model_history_visible_run

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.history.types import HistoryScope

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
