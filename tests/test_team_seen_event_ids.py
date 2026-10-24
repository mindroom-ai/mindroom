"""Seen-event bookkeeping after a team run must not clobber what the run stored."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.storage import _with_scope_state, read_scope_seen_event_ids, read_scope_state
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession, session_state_snapshot
from mindroom.teams.envelope import _persist_bound_seen_event_ids

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_seen_event_ids_are_written_onto_the_stored_row_not_the_pre_run_snapshot(tmp_path: Path) -> None:
    """The post-run write merges into the row, so the state the run wrote back and a compaction request survive."""
    storage = create_state_storage("eng", tmp_path)
    scope = HistoryScope(kind="team", scope_id="eng")
    stored = ConversationSession(
        session_id="t1",
        kind="team",
        owner_id="eng",
        metadata={},
        session_state={"phase": "before"},
        created_at=1,
        updated_at=1,
    )
    storage.ensure_session_row(stored)
    snapshot = deepcopy(stored)
    # What the run does behind the snapshot's back: its tools change the shared state and the run writes it back.
    stored.session_state = {"phase": "after"}
    storage.save_session_state(stored, session_state_snapshot(stored.session_state))
    # The user asks for compaction while the run finishes.
    storage.update_metadata(
        stored,
        lambda metadata: _with_scope_state(metadata, scope, HistoryScopeState(force_compact_before_next_run=True)),
    )

    await _persist_bound_seen_event_ids(
        scope_context=ScopeSessionContext(scope=scope, storage=storage, session=snapshot, session_id="t1"),
        event_ids=["$reply", "$unseen"],
    )
    reloaded = storage.load_session("t1", "team")
    assert reloaded is not None
    seen_event_ids = read_scope_seen_event_ids(storage, reloaded, scope)

    assert reloaded.session_state == {"phase": "after"}
    assert read_scope_state(reloaded, scope).force_compact_before_next_run is True
    assert seen_event_ids == {"$reply", "$unseen"}
