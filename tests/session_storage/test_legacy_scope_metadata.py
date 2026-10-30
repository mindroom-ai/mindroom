"""History state an earlier release kept per scope in session metadata becomes the owner's one value, once.

``tests/fixtures/history_metadata/v2026.10.696.json`` holds an agent's and a team's conversation databases as
v2026.10.696's own writers left them: a force-compaction request, preserved seen ids, both, and neither.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from mindroom.constants import MINDROOM_HISTORY_METADATA_KEY
from mindroom.history.storage import has_queued_force_compaction, read_scope_seen_event_ids, read_scope_state
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.schema import LOG_VERSION, TableNames
from mindroom.session_storage.store import ConversationStore

_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "history_metadata" / "v2026.10.696.json").read_text(encoding="utf-8"),
)
_OWNERS = {"agent": HistoryScope(kind="agent", scope_id="code"), "team": HistoryScope(kind="team", scope_id="crew")}
# What the release's readers read of each recorded session: whether it asked for compaction, and its preserved seen ids.
_RECORDED = {
    "forced": (True, ["$a", "$b"]),
    "seen_only": (False, ["$c"]),
    "forced_only": (True, []),
    "plain": (False, []),
}


def _restore(kind: str, root: Path) -> Path:
    owner = _OWNERS[kind]
    database = TableNames(owner.scope_id).database_path(root)
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("\n".join(_FIXTURE["databases"][kind]))
    return database


def _open(kind: str, root: Path) -> ConversationStore:
    owner = _OWNERS[kind]
    store = create_state_storage(owner.scope_id, root, owner)
    assert isinstance(store, ConversationStore)
    return store


def _metadata(database: Path, session_id: str) -> str:
    with closing(sqlite3.connect(database)) as connection:
        (metadata,) = connection.execute(
            f'SELECT metadata FROM "{database.stem}_sessions" WHERE session_id = ?',  # noqa: S608
            (session_id,),
        ).fetchone()
    return metadata


def _set_metadata(database: Path, session_id: str, metadata: dict[str, Any]) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            f'UPDATE "{database.stem}_sessions" SET metadata = ? WHERE session_id = ?',  # noqa: S608
            (json.dumps(metadata), session_id),
        )
        connection.commit()


@pytest.mark.parametrize("kind", sorted(_OWNERS))
def test_every_recorded_session_reads_as_the_release_that_wrote_it_read_it(kind: str, tmp_path: Path) -> None:
    """The owner's request and seen ids move into one value; the row's other metadata stays."""
    database = _restore(kind, tmp_path)
    plain = _metadata(database, "plain")

    store = _open(kind, tmp_path)

    for session_id, (forced, seen) in _RECORDED.items():
        session = store.load_session(session_id)
        assert session is not None
        assert read_scope_state(session).force_compact_before_next_run is forced
        assert has_queued_force_compaction(store, session_id) is forced
        assert read_scope_seen_event_ids(store, session) == set(seen)
        state = {
            **({"force_compact_before_next_run": True} if forced else {}),
            **({"seen_event_ids": seen} if seen else {}),
        }
        assert session.metadata == {"other": "kept", **({MINDROOM_HISTORY_METADATA_KEY: state} if state else {})}
    assert _metadata(database, "plain") == plain


def test_other_scopes_and_unread_versions_are_dropped_and_a_newer_flag_is_kept(tmp_path: Path) -> None:
    """Only the owner's entries were ever read; a flag the conversion already wrote in the new value stays set."""
    database = _restore("agent", tmp_path)
    _set_metadata(
        database,
        "forced",
        {
            "mindroom_compaction": {
                "version": 2,
                "states": {"team:crew": {"force_compact_before_next_run": True}, "agent:code": {"other": 1}},
            },
            "mindroom_matrix_history": {
                "version": 1,
                "states": {"agent:code": {"seen_event_ids": ["$z", "", 3]}, "agent:other": {"seen_event_ids": ["$x"]}},
            },
            MINDROOM_HISTORY_METADATA_KEY: {"force_compact_before_next_run": True},
        },
    )
    _set_metadata(
        database,
        "seen_only",
        {
            "mindroom_compaction": {"version": 1, "force_compact_before_next_run": True},
            "mindroom_matrix_history": {"version": 2, "states": {"agent:code": {"seen_event_ids": ["$c"]}}},
        },
    )

    store = _open("agent", tmp_path)

    forced = store.load_session("forced")
    assert forced is not None
    assert forced.metadata == {
        MINDROOM_HISTORY_METADATA_KEY: {"seen_event_ids": ["$z"], "force_compact_before_next_run": True},
    }
    seen_only = store.load_session("seen_only")
    assert seen_only is not None
    assert seen_only.metadata == {}


def test_a_converted_database_converts_once(tmp_path: Path) -> None:
    """The layout version records the step, so a later open reads the rows as they are."""
    database = _restore("team", tmp_path)
    _open("team", tmp_path)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute('SELECT version FROM "crew_sessions_log_meta"').fetchall() == [(LOG_VERSION,)]
    _set_metadata(database, "plain", {"mindroom_compaction": {"version": 2, "states": {"team:crew": {}}}})

    _open("team", tmp_path)

    assert json.loads(_metadata(database, "plain")) == {
        "mindroom_compaction": {"version": 2, "states": {"team:crew": {}}},
    }
