"""An agent's session rows that name another owner take the agent's name once, at its store's open or the sweep."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.session_storage.agent_storage import create_agent_state_storage, create_state_storage
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_storage
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames
from mindroom.session_storage.schema import LOG_VERSION, TableNames
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.storage import SYSTEM_USAGE_ENTITY, SYSTEM_USAGE_STORAGE_NAME, quote_identifier
from tests.conftest import seed_session
from tests.history_helpers import compact_runs, stored_run_ids
from tests.session_storage.agno_layout import write_agno_database

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore

_SCOPE = HistoryScope(kind="agent", scope_id="code")
# The id Agno derived from the display name ``Code Agent`` before MindRoom passed the agent's name.
_DISPLAY_NAME_ID = "code-agent"


def _run(run_id: str, session_id: str = "session") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=_DISPLAY_NAME_ID,
        session_id=session_id,
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}")],
    )


def _session(session_id: str, owner_id: str, *, kind: str = "agent") -> ConversationSession:
    return ConversationSession(session_id=session_id, kind=kind, owner_id=owner_id, runs=[_run(f"{session_id}-run")])


def _v2_database(directory: Path, *sessions: ConversationSession) -> Path:
    """Write ``sessions`` into the storage v2 database of the store directory ``directory``, at layout version 2."""
    store = create_state_storage(directory.name, directory)
    for session in sessions:
        seed_session(store, session)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute(f"UPDATE {quote_identifier(store.names.log_meta)} SET version = 2")  # noqa: S608
    return store.path


def _agno_database(directory: Path, *sessions: ConversationSession) -> Path:
    return write_agno_database(AgnoTableNames(directory.name).database_path(directory), *sessions, usage=False)


def _rows(database: Path) -> tuple[int, list[tuple[str, str, str | None, str | None]]]:
    """Return the layout version and each session row's id, kind, agent, and team."""
    names = TableNames.of_database(database)
    with closing(sqlite3.connect(database)) as connection:
        (version,) = connection.execute(f"SELECT version FROM {quote_identifier(names.log_meta)}").fetchone()  # noqa: S608
        rows = connection.execute(
            f"SELECT session_id, session_type, agent_id, team_id FROM {quote_identifier(names.sessions)} "  # noqa: S608
            "ORDER BY session_id",
        ).fetchall()
    return version, rows


def test_an_agents_open_converts_rows_naming_another_owner_to_the_agent(tmp_path: Path) -> None:
    """A converted row naming the display-name id, or no owner, names the agent; its runs keep their id."""
    database = _agno_database(tmp_path / "code", _session("old", _DISPLAY_NAME_ID), _session("unowned", ""))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE code_sessions SET agent_id = NULL WHERE session_id = 'unowned'")

    store = create_agent_state_storage("code", tmp_path / "code")

    assert _rows(store.path) == (LOG_VERSION, [("old", "agent", "code", None), ("unowned", "agent", "code", None)])
    session = store.load_session("old", "agent")
    assert session is not None
    assert [run.agent_id for run in session.runs] == [_DISPLAY_NAME_ID]


def test_a_storage_v2_database_adopts_its_rows_once_at_its_agents_open(tmp_path: Path) -> None:
    """Only the agent's open upgrades a version 2 database, and an open after that reads only the version."""
    database = _v2_database(tmp_path / "code", _session("old", _DISPLAY_NAME_ID))

    create_state_storage("code", tmp_path / "code")
    assert _rows(database) == (2, [("old", "agent", _DISPLAY_NAME_ID, None)])

    create_agent_state_storage("code", tmp_path / "code")
    assert _rows(database) == (LOG_VERSION, [("old", "agent", "code", None)])

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE code_sessions SET agent_id = 'planted'")
    create_agent_state_storage("code", tmp_path / "code")
    assert _rows(database)[1] == [("old", "agent", "planted", None)]


@pytest.mark.asyncio
async def test_the_startup_sweep_adopts_agent_rows_and_leaves_team_and_system_rows(tmp_path: Path) -> None:
    """Agents' databases, shared or private, adopt their rows; a team's and the system usage database keep theirs."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    root = paths.storage_root
    shared = _v2_database(root / "agents/code", _session("old", _DISPLAY_NAME_ID))
    private = _agno_database(root / "private_instances/scope/code", _session("old", _DISPLAY_NAME_ID))
    # A team bot's turn on an agent path saves an agent row its team scope owns.
    team = _v2_database(
        root / "teams/team_crew_000000000000",
        _session("team-row", "crew", kind="team"),
        _session("agent-row", "crew"),
    )
    system = _v2_database(root / SYSTEM_USAGE_STORAGE_NAME, _session("usage", SYSTEM_USAGE_ENTITY))

    await migrate_conversation_storage(paths)

    assert _rows(shared) == (LOG_VERSION, [("old", "agent", "code", None)])
    assert _rows(private) == (LOG_VERSION, [("old", "agent", "code", None)])
    assert _rows(team) == (LOG_VERSION, [("agent-row", "agent", "crew", None), ("team-row", "team", None, "crew")])
    assert _rows(system) == (LOG_VERSION, [("usage", "agent", SYSTEM_USAGE_ENTITY, None)])


def test_a_compaction_after_the_adoption_replays_its_summary(tmp_path: Path) -> None:
    """A conversation last saved under the display-name id keeps its summary and compacted run after a compaction."""
    _agno_database(tmp_path / "code", _session("session", _DISPLAY_NAME_ID))
    store: ConversationStore = create_agent_state_storage("code", tmp_path / "code")
    session = store.load_session("session", "agent")
    assert session is not None
    new_run = RunRecord(
        run_id="new",
        agent_id="code",
        session_id="session",
        content="answer new",
        messages=[Message(role="user", content="question new")],
    )
    store.save_run(session, new_run, record_usage=False)

    compact_runs(store, session, _SCOPE, ["new"], "summary of new")

    reloaded = store.load_session("session", "agent")
    assert reloaded is not None
    assert reloaded.summary is not None
    assert reloaded.summary.text == "summary of new"
    assert [run.run_id for run in reloaded.runs] == ["session-run"]
    assert stored_run_ids(store, "session") == ["session-run", "new"]
