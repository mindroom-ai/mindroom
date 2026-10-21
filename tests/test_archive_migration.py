"""A compaction archive written by a released MindRoom becomes summary rows once, when its database opens."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session
from tests.history_helpers import folded_run_ids, stored_run_ids

if TYPE_CHECKING:
    from pathlib import Path

_SCOPE = HistoryScope(kind="agent", scope_id="code")

# The archive layout every release since #2351 created beside ``code_sessions``.
_ARCHIVE_DDL = (
    "CREATE TABLE code_sessions_compactions (id INTEGER PRIMARY KEY, "
    "session_id TEXT NOT NULL REFERENCES code_sessions(session_id) ON DELETE CASCADE, "
    "scope_key TEXT NOT NULL, summary TEXT, summary_model TEXT, "
    "legacy INTEGER NOT NULL DEFAULT 0, legacy_event_ids TEXT, created_at INTEGER NOT NULL)",
    "CREATE TABLE code_sessions_compacted_runs (id INTEGER PRIMARY KEY, "
    "compaction_id INTEGER NOT NULL REFERENCES code_sessions_compactions(id) ON DELETE CASCADE, "
    "session_id TEXT NOT NULL, run_id TEXT NOT NULL, run_type TEXT, event_ids TEXT NOT NULL, "
    "seen_event_ids TEXT NOT NULL DEFAULT '[]', run_data TEXT, UNIQUE(session_id, run_id))",
)


@dataclass
class _Generation:
    """One archive generation as a released archive stored it."""

    summary: str | None
    runs: list[RunRecord | TeamRunRecord] = field(default_factory=list)
    tombstones: tuple[str, ...] = ()
    legacy: bool = False
    legacy_event_ids: tuple[str, ...] = ()
    scope_key: str = _SCOPE.key


def _run(run_id: str, *, agent_id: str = "code", content: str | None = None) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        session_id="s1",
        status=RunStatus.completed,
        content=content or f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}")],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"],
        },
    )


def _db(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / "code.db"


def _open(tmp_path: Path) -> ConversationStore:
    storage = create_state_storage("code", tmp_path, session_table="code_sessions")
    assert isinstance(storage, ConversationStore)
    return storage


def _main_database(
    tmp_path: Path,
    *,
    live: list[RunRecord | TeamRunRecord],
    generations: list[_Generation],
    team: bool = False,
    blob: list[dict[str, object]] | None = None,
) -> None:
    """Write a conversation database as a release with the compaction archive left it."""
    storage = _open(tmp_path)
    session = (
        ConversationSession(kind="team", session_id="s1", owner_id="eng", runs=live)
        if team
        else ConversationSession(kind="agent", session_id="s1", owner_id="code", runs=live)
    )
    seed_session(storage, session)
    with closing(sqlite3.connect(_db(tmp_path))) as connection, connection:
        connection.execute("DROP TABLE IF EXISTS code_sessions_summaries")
        connection.execute("DROP TABLE IF EXISTS code_sessions_removed_runs")
        for statement in _ARCHIVE_DDL:
            connection.execute(statement)
        for generation in generations:
            generation_id = connection.execute(
                "INSERT INTO code_sessions_compactions "
                "(session_id, scope_key, summary, summary_model, legacy, legacy_event_ids, created_at) "
                "VALUES ('s1', ?, ?, 'summary-model', ?, ?, ?)",
                (
                    generation.scope_key,
                    generation.summary,
                    int(generation.legacy),
                    json.dumps(list(generation.legacy_event_ids)),
                    int(time.time()),
                ),
            ).lastrowid
            for run in generation.runs:
                visible = run.parent_run_id is None
                # Redaction also matches events a run only read; those never count as seen.
                seen_event_ids = [f"${run.run_id}"] if visible else []
                event_ids = [*seen_event_ids, f"$read-by-{run.run_id}"] if visible else []
                connection.execute(
                    "INSERT INTO code_sessions_compacted_runs "
                    "(compaction_id, session_id, run_id, run_type, event_ids, seen_event_ids, run_data) "
                    "VALUES (?, 's1', ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        run.run_id,
                        "team" if isinstance(run, TeamRunRecord) else "agent",
                        json.dumps(event_ids),
                        json.dumps(seen_event_ids),
                        json.dumps(run.to_dict()),
                    ),
                )
            for run_id in generation.tombstones:
                connection.execute(
                    "INSERT INTO code_sessions_compacted_runs (compaction_id, session_id, run_id, event_ids) "
                    "VALUES (?, 's1', ?, '[]')",
                    (generation_id, run_id),
                )
        if blob is not None:
            # Databases Agno 2 created keep their ``runs`` blob column.
            connection.execute("ALTER TABLE code_sessions ADD COLUMN runs JSON")
            connection.execute(
                "UPDATE code_sessions SET runs = ? WHERE session_id = 's1'",
                (json.dumps(json.dumps(blob)),),
            )


def _tables(tmp_path: Path) -> set[str]:
    with closing(sqlite3.connect(_db(tmp_path))) as connection, connection:
        return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


@dataclass(frozen=True)
class _Replay:
    loaded: list[str]
    summary: str | None
    seen: set[str]
    stored: list[str]
    folded: list[str]


def _replay(tmp_path: Path) -> _Replay:
    storage = _open(tmp_path)
    session = storage.load_session("s1", "agent")
    assert session is not None
    return _Replay(
        loaded=[run.run_id or "" for run in session.runs or []],
        summary=session.summary.text if session.summary is not None else None,
        seen=read_scope_seen_event_ids(storage, session, _SCOPE),
        stored=stored_run_ids(storage, "s1"),
        folded=folded_run_ids(storage, "s1"),
    )


def test_ordinary_generations_become_summary_rows_after_their_runs(tmp_path: Path) -> None:
    """Archived runs return to the runs table ahead of the live ones, and each generation sorts after its runs."""
    _main_database(
        tmp_path,
        live=[_run("r2")],
        generations=[_Generation("summary of r0", [_run("r0")]), _Generation("summary of r0 and r1", [_run("r1")])],
    )

    replay = _replay(tmp_path)

    assert replay == _Replay(
        loaded=["r2"],
        summary="summary of r0 and r1",
        seen={"$r0", "$r1", "$r2"},
        stored=["r0", "r1", "r2"],
        folded=["r0", "r1"],
    )
    assert "code_sessions_compactions" not in _tables(tmp_path)
    assert "code_sessions_compacted_runs" not in _tables(tmp_path)


def test_reopening_a_migrated_database_changes_nothing(tmp_path: Path) -> None:
    """The migration runs once; the summary-row tables mark it done."""
    _main_database(tmp_path, live=[_run("r1")], generations=[_Generation("summary of r0", [_run("r0")])])

    first = _replay(tmp_path)
    second = _replay(tmp_path)

    assert first == second
    assert first.loaded == ["r1"]


def test_a_legacy_generation_stays_in_force_before_every_run(tmp_path: Path) -> None:
    """A content-free legacy generation becomes a legacy row before every run, still in force."""
    _main_database(
        tmp_path,
        live=[_run("r1")],
        generations=[
            _Generation("legacy summary", tombstones=("gone",), legacy=True, legacy_event_ids=("$legacy",)),
            _Generation("legacy summary and r0", [_run("r0")]),
        ],
    )

    replay = _replay(tmp_path)
    storage = _open(tmp_path)
    legacy = summary_rows.legacy_event_ids(storage, session_id="s1", scope_key=_SCOPE.key)

    assert replay.summary == "legacy summary and r0"
    assert replay.loaded == ["r1"]
    assert replay.seen == {"$legacy", "$r0", "$r1"}
    assert legacy == {"$legacy"}


def test_a_barrier_generation_retires_what_came_before(tmp_path: Path) -> None:
    """A generation without a summary keeps earlier archived runs out of replay and out of the seen ids."""
    _main_database(
        tmp_path,
        live=[_run("r1")],
        generations=[_Generation("summary of r0", [_run("r0")]), _Generation(None, tombstones=("t5",))],
    )

    replay = _replay(tmp_path)

    assert replay.summary is None
    assert replay.loaded == ["r1"]
    assert replay.seen == {"$r1"}
    assert replay.stored == ["r0", "r1"]


def test_retired_generations_keep_their_runs_behind_the_barrier(tmp_path: Path) -> None:
    """A retired generation keeps its archived runs stored, but neither replays nor counts them."""
    _main_database(tmp_path, live=[_run("r1")], generations=[_Generation(None, [_run("r0")])])

    replay = _replay(tmp_path)

    assert replay.summary is None
    assert replay.loaded == ["r1"]
    assert replay.seen == {"$r1"}
    assert replay.stored == ["r0", "r1"]


def test_an_empty_keeper_generation_sorts_before_every_run(tmp_path: Path) -> None:
    """A generation left with only tombstones and no earlier generation hides nothing."""
    _main_database(tmp_path, live=[_run("r0")], generations=[_Generation(None, tombstones=("gone",))])

    replay = _replay(tmp_path)

    assert replay.summary is None
    assert replay.loaded == ["r0"]
    assert replay.stored == ["r0"]


def test_stale_copies_of_archived_and_tombstoned_runs_are_dropped(tmp_path: Path) -> None:
    """A live copy that reconcile had not pruned yet never replays next to the summary that covers it."""
    _main_database(
        tmp_path,
        live=[_run("r0", content="stale live copy"), _run("t1"), _run("r2")],
        generations=[_Generation("summary of r0", [_run("r0")], tombstones=("t1",))],
    )

    replay = _replay(tmp_path)
    storage = _open(tmp_path)
    restored = storage.get_run("r0")

    assert replay.loaded == ["r2"]
    assert replay.stored == ["r0", "r2"]
    assert isinstance(restored, RunRecord)
    assert restored.content == "answer r0"


def test_blob_residue_of_an_archived_run_is_dropped(tmp_path: Path) -> None:
    """A blob entry of an archived run that a swallowed scrub error left behind does not come back."""
    blob_copy = {**_run("r0", content="stale blob copy").to_dict(), "session_id": "s1"}
    _main_database(
        tmp_path,
        live=[_run("r1")],
        generations=[_Generation("summary of r0", [_run("r0")])],
        blob=[blob_copy],
    )

    replay = _replay(tmp_path)

    assert replay.loaded == ["r1"]
    assert replay.stored == ["r0", "r1"]
    assert replay.summary == "summary of r0"


def test_an_interrupted_compaction_keeps_visible_blob_runs_after_its_summary(tmp_path: Path) -> None:
    """Blob runs a failed later chunk left visible keep replaying after the chunk that did commit."""
    blob = [{**_run(run_id).to_dict(), "session_id": "s1"} for run_id in ("b1", "b2")]
    _main_database(tmp_path, live=[], generations=[_Generation("summary of b0", [_run("b0")])], blob=blob)

    replay = _replay(tmp_path)

    assert replay.stored == ["b0", "b1", "b2"]
    assert replay.loaded == ["b1", "b2"]
    assert replay.summary == "summary of b0"


def test_archived_member_runs_return_with_their_team_run(tmp_path: Path) -> None:
    """A team run archived with its members is folded with them behind its summary row."""
    member = RunRecord(run_id="m0", agent_id="helper", session_id="s1", parent_run_id="t0")
    team_run = TeamRunRecord(run_id="t0", team_id="eng", session_id="s1", status=RunStatus.completed)
    later = TeamRunRecord(run_id="t1", team_id="eng", session_id="s1", status=RunStatus.completed)
    _main_database(
        tmp_path,
        live=[later],
        generations=[_Generation("team summary", [member, team_run], scope_key="team:eng")],
        team=True,
    )

    storage = _open(tmp_path)
    session = storage.load_session("s1", "team")
    stored = stored_run_ids(storage, "s1")
    folded = folded_run_ids(storage, "s1")

    assert session is not None
    assert [run.run_id for run in session.runs or []] == ["t1"]
    assert session.summary is not None
    assert session.summary.text == "team summary"
    assert stored == ["m0", "t0", "t1"]
    assert folded == ["t0"]


def test_rows_without_an_index_keep_their_place(tmp_path: Path) -> None:
    """Rows an old writer stored without an index keep the place a read gave them: first among the live runs."""
    _main_database(tmp_path, live=[_run("r1"), _run("r2")], generations=[_Generation("summary of r0", [_run("r0")])])
    with closing(sqlite3.connect(_db(tmp_path))) as connection, connection:
        connection.execute("UPDATE code_sessions_runs SET run_index = NULL WHERE run_id = 'r2'")

    replay = _replay(tmp_path)

    assert replay.stored == ["r0", "r2", "r1"]
    assert replay.loaded == ["r2", "r1"]


def test_two_scopes_keep_separate_summary_rows(tmp_path: Path) -> None:
    """Each scope's runs load after that scope's own latest summary row."""
    _main_database(
        tmp_path,
        live=[_run("c1"), _run("o1", agent_id="other")],
        generations=[
            _Generation("code summary", [_run("c0")]),
            _Generation("other summary", [_run("o0", agent_id="other")], scope_key="agent:other"),
        ],
    )

    replay = _replay(tmp_path)

    assert replay.stored == ["c0", "o0", "c1", "o1"]
    assert replay.loaded == ["c1", "o1"]
    assert replay.summary == "code summary"


def test_redaction_candidates_keep_what_the_archive_matched(tmp_path: Path) -> None:
    """Rows fold the event ids redaction matched archived runs by, and a legacy generation's captured ids."""
    _main_database(
        tmp_path,
        live=[_run("r1")],
        generations=[
            _Generation("legacy summary", legacy=True, legacy_event_ids=("$legacy",)),
            _Generation("legacy summary and r0", [_run("r0")]),
        ],
    )

    storage = _open(tmp_path)
    candidates = summary_rows.history_event_ids(storage, session_id="s1", scope_key=_SCOPE.key, kind="all")
    seen = summary_rows.history_event_ids(storage, session_id="s1", scope_key=_SCOPE.key, kind="seen")

    assert candidates == {"$legacy", "$r0", "$read-by-r0"}
    assert seen == {"$legacy", "$r0"}


def test_redacting_an_archived_message_after_migration_cuts_from_its_run(tmp_path: Path) -> None:
    """A restored run still carries its provenance, so redaction finds it and removes its summary."""
    _main_database(
        tmp_path,
        live=[_run("r2")],
        generations=[_Generation("summary of r0", [_run("r0")]), _Generation("summary of r0 and r1", [_run("r1")])],
    )
    storage = _open(tmp_path)
    assert summary_rows.cut_at_event(storage, session_id="s1", scope_key=_SCOPE.key, event_id="$r1", match="any")

    replay = _replay(tmp_path)

    assert replay.summary == "summary of r0"
    assert replay.stored == ["r0"]
    assert replay.loaded == []
