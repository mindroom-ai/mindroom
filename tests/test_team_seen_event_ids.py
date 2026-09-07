"""Seen-event bookkeeping after a team run must not clobber what agno persisted during it."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from agno.session.team import TeamSession

from mindroom.agent_storage import create_state_storage, get_team_session
from mindroom.history.runtime import ScopeSessionContext
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.history.types import HistoryScope
from mindroom.teams import _persist_bound_seen_event_ids
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path


def test_seen_event_ids_are_written_onto_the_stored_row_not_the_pre_run_snapshot(tmp_path: Path) -> None:
    """The post-run write re-reads the row so agno's newer session_data survives."""
    storage = create_state_storage("eng", tmp_path, subdir="sessions", session_table="eng_sessions")
    scope = HistoryScope(kind="team", scope_id="eng")
    try:
        stored = seed_session(
            storage,
            TeamSession(
                session_id="t1",
                team_id="eng",
                session_data={"session_state": {"phase": "before"}},
                metadata={},
                runs=[],
                created_at=1,
                updated_at=1,
            ),
        )
        snapshot = deepcopy(stored)
        # What agno does inside arun, behind the snapshot's back.
        stored.session_data = {"session_state": {"phase": "after"}}
        storage.upsert_session(stored)

        _persist_bound_seen_event_ids(
            scope_context=ScopeSessionContext(scope=scope, storage=storage, session=snapshot),
            session_id="t1",
            event_ids=["$reply", "$unseen"],
        )
        reloaded = get_team_session(storage, "t1")
    finally:
        storage.close()

    assert reloaded is not None
    assert reloaded.session_data == {"session_state": {"phase": "after"}}
    assert read_scope_seen_event_ids(reloaded, scope) == {"$reply", "$unseen"}
