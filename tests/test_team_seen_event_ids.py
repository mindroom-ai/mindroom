"""Seen-event bookkeeping after a team run must not clobber what the run stored."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.teams.envelope import _persist_bound_seen_event_ids

if TYPE_CHECKING:
    from pathlib import Path


def test_seen_event_ids_are_written_onto_the_stored_row_not_the_pre_run_snapshot(tmp_path: Path) -> None:
    """The post-run write re-reads the row, so the session state the run wrote back survives."""
    storage = create_state_storage("eng", tmp_path, session_table="eng_sessions")
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
    storage.save_session_fields(stored, "metadata", "session_state")
    snapshot = deepcopy(stored)
    # What the run does behind the snapshot's back: its tools change the shared state and the run writes it back.
    stored.session_state = {"phase": "after"}
    storage.save_session_fields(stored, "session_state")

    _persist_bound_seen_event_ids(
        scope_context=ScopeSessionContext(scope=scope, storage=storage, session=snapshot),
        session_id="t1",
        event_ids=["$reply", "$unseen"],
    )
    reloaded = storage.load_session("t1", "team")
    assert reloaded is not None
    seen_event_ids = read_scope_seen_event_ids(storage, reloaded, scope)

    assert reloaded.session_state == {"phase": "after"}
    assert seen_event_ids == {"$reply", "$unseen"}
