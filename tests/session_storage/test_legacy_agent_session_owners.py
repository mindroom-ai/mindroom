"""An agent's session rows that name another owner take the agent's name once, at its store's open or the sweep."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, resolve_runtime_paths
from mindroom.history.run_visibility import replays_in_scope
from mindroom.history.storage import remove_history_of_redacted_events
from mindroom.history.types import HistoryScope, ScopeSummary
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.session_storage.agent_storage import create_agent_state_storage, create_state_storage
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_storage
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames
from mindroom.session_storage.schema import LOG_VERSION, TableNames, quote_identifier
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.storage import SYSTEM_USAGE_ENTITY, SYSTEM_USAGE_OWNER, SYSTEM_USAGE_STORAGE_NAME
from tests.conftest import seed_session
from tests.history_helpers import compact_runs, stored_run_ids, summary_rows_of
from tests.session_storage.agno_layout import write_agno_database

if TYPE_CHECKING:
    from collections.abc import Collection
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
        metadata={MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}"},
    )


def _session(session_id: str, owner_id: str, *, kind: str = "agent") -> ConversationSession:
    return ConversationSession(session_id=session_id, kind=kind, owner_id=owner_id, runs=[_run(f"{session_id}-run")])


def _v2_database(directory: Path, *sessions: ConversationSession, owner: HistoryScope = _SCOPE) -> Path:
    """Write ``sessions`` into the storage v2 database ``owner`` owns in the store directory ``directory``."""
    store = create_state_storage(directory.name, directory, owner)
    for session in sessions:
        seed_session(store, session)
    return _as_layout_2(store, *sessions)


def _as_layout_2(store: ConversationStore, *sessions: ConversationSession) -> Path:
    """Rewrite ``store``'s database in storage v2's layout at version 2, its rows naming ``sessions``' owners.

    That layout's session rows named their kind and owner and kept their state inside ``session_data``, and its meta
    row named no owner.
    """
    names = store.names
    table, meta = quote_identifier(names.sessions), quote_identifier(names.log_meta)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        for column in ("session_type VARCHAR NOT NULL DEFAULT 'agent'", "agent_id VARCHAR", "team_id VARCHAR"):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        for session in sessions:
            connection.execute(
                f"UPDATE {table} SET session_type = ?, {'team_id' if session.kind == 'team' else 'agent_id'} = ? "  # noqa: S608
                "WHERE session_id = ?",
                (session.kind, session.owner_id, session.session_id),
            )
        connection.execute(
            f"UPDATE {table} SET session_data = json_set(COALESCE(session_data, '{{}}'), '$.session_state', "  # noqa: S608
            "json(session_state))",
        )
        connection.execute(f"ALTER TABLE {table} DROP COLUMN session_state")
        for column in ("owner_kind", "owner_id"):
            connection.execute(f"ALTER TABLE {meta} DROP COLUMN {column}")
        connection.execute(f"UPDATE {meta} SET version = 2")  # noqa: S608
    return store.path


def _agno_database(directory: Path, *sessions: ConversationSession) -> Path:
    return write_agno_database(AgnoTableNames(directory.name).database_path(directory), *sessions, usage=False)


def _owner(database: Path) -> tuple[int, HistoryScope]:
    """Return the layout version and the owner the database's meta row records."""
    names = TableNames.of_database(database)
    with closing(sqlite3.connect(database)) as connection:
        version, kind, owner_id = connection.execute(
            f"SELECT version, owner_kind, owner_id FROM {quote_identifier(names.log_meta)}",  # noqa: S608
        ).fetchone()
    return version, HistoryScope(kind=kind, scope_id=owner_id)


def _run_agents(database: Path) -> dict[str, str]:
    """Return each stored run's agent, by run id."""
    names = TableNames.of_database(database)
    with closing(sqlite3.connect(database)) as connection:
        return dict(
            connection.execute(
                f"SELECT run_id, json_extract(record, '$.agent_id') FROM {quote_identifier(names.log_runs)}",  # noqa: S608
            ),
        )


def test_an_agents_open_converts_rows_naming_another_owner_to_the_agent(tmp_path: Path) -> None:
    """A converted session whose row named the display-name id, or no owner, is the agent's, and so are its runs."""
    database = _agno_database(tmp_path / "code", _session("old", _DISPLAY_NAME_ID), _session("unowned", ""))
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE code_sessions SET agent_id = NULL WHERE session_id = 'unowned'")

    store = create_agent_state_storage("code", tmp_path / "code")

    assert _owner(store.path) == (LOG_VERSION, _SCOPE)
    for session_id in ("old", "unowned"):
        session = store.load_session(session_id)
        assert session is not None
        assert session.owner_id == "code"
        assert [run.agent_id for run in session.runs] == ["code"]


def test_a_storage_v2_database_adopts_its_rows_once_at_its_agents_open(tmp_path: Path) -> None:
    """The agent's open upgrades a version 2 database once, and an open after that reads only the version."""
    database = _v2_database(tmp_path / "code", _session("old", _DISPLAY_NAME_ID))
    assert _run_agents(database) == {"old-run": _DISPLAY_NAME_ID}

    create_agent_state_storage("code", tmp_path / "code")
    assert (_owner(database), _run_agents(database)) == ((LOG_VERSION, _SCOPE), {"old-run": "code"})

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE code_sessions_log_runs SET record = json_set(record, '$.agent_id', 'planted')")
    create_agent_state_storage("code", tmp_path / "code")
    assert _run_agents(database) == {"old-run": "planted"}


@pytest.mark.asyncio
async def test_the_startup_sweep_adopts_agent_rows_and_leaves_team_and_system_rows(tmp_path: Path) -> None:
    """Agents' databases, shared or private, adopt their rows; a team's and the system usage database keep theirs."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    root = paths.storage_root
    shared = _v2_database(root / "agents/code", _session("old", _DISPLAY_NAME_ID))
    private = _agno_database(root / "private_instances/scope/code", _session("old", _DISPLAY_NAME_ID))
    # A team bot's turn on an agent path saves an agent row its team scope owns.
    crew = HistoryScope(kind="team", scope_id="crew")
    team = _v2_database(
        root / "teams" / TableNames.of_team(crew).storage_name,
        _session("team-row", "crew", kind="team"),
        _session("agent-row", "crew"),
        owner=crew,
    )
    system = _v2_database(
        root / SYSTEM_USAGE_STORAGE_NAME,
        _session("usage", SYSTEM_USAGE_ENTITY),
        owner=SYSTEM_USAGE_OWNER,
    )

    await migrate_conversation_storage(paths)

    assert (_owner(shared), _run_agents(shared)) == ((LOG_VERSION, _SCOPE), {"old-run": "code"})
    assert (_owner(private), _run_agents(private)) == ((LOG_VERSION, _SCOPE), {"old-run": "code"})
    assert (_owner(team), _run_agents(team)) == (
        (LOG_VERSION, crew),
        {"team-row-run": _DISPLAY_NAME_ID, "agent-row-run": _DISPLAY_NAME_ID},
    )
    assert _owner(system) == (LOG_VERSION, SYSTEM_USAGE_OWNER)


def test_a_compaction_after_the_adoption_replays_its_summary(tmp_path: Path) -> None:
    """A conversation started under the display-name id keeps its summary and compacted run after a compaction."""
    _agno_database(tmp_path / "code", _session("session", _DISPLAY_NAME_ID))
    store: ConversationStore = create_agent_state_storage("code", tmp_path / "code")
    session = store.load_session("session")
    assert session is not None
    new_run = RunRecord(
        run_id="new",
        agent_id="code",
        session_id="session",
        content="answer new",
        messages=[Message(role="user", content="question new")],
    )
    store.save_run(session, new_run, record_usage=False)

    compact_runs(store, session, ["new"], "summary of new")

    reloaded = store.load_session("session")
    assert reloaded is not None
    assert reloaded.summary is not None
    assert reloaded.summary.text == "summary of new"
    assert "new" not in [run.run_id for run in reloaded.runs]
    assert stored_run_ids(store, "session") == ["session-run", "new"]


def test_a_conversation_continued_after_v2026_2_55_replays_its_earlier_runs(tmp_path: Path) -> None:
    """Runs saved under the display-name id replay as the agent's history beside the runs saved since.

    A run another agent ran for one of them keeps its own agent.
    """
    session = _session("session", "code")
    session.runs.append(replace(_run("new"), agent_id="code"))
    session.runs.append(replace(_run("child"), agent_id="helper", parent_run_id="new"))
    _v2_database(tmp_path / "code", session)

    store = create_agent_state_storage("code", tmp_path / "code")

    loaded = store.load_session("session")
    assert loaded is not None
    assert [run.run_id for run in loaded.runs if replays_in_scope(run, _SCOPE)] == ["session-run", "new"]
    assert [run.agent_id for run in loaded.runs if run.parent_run_id is not None] == ["helper"]


@pytest.mark.asyncio
async def test_redacting_an_event_an_adopted_run_answered_cuts_from_that_run(tmp_path: Path) -> None:
    """A redaction cleanup finds and cuts a run saved under the display-name id, with every run after it."""
    session = _session("session", "code")
    session.runs.append(replace(_run("new"), agent_id="code"))
    _v2_database(tmp_path / "code", session)
    store = create_agent_state_storage("code", tmp_path / "code")
    loaded = store.load_session("session")
    assert loaded is not None

    async def redacted_history_events(event_ids: Collection[str]) -> dict[str, str | None]:
        return dict.fromkeys(set(event_ids) & {"$session-run"})

    assert await remove_history_of_redacted_events(store, loaded, redacted_history_events) == ["$session-run"]
    assert stored_run_ids(store, "session") == []


def test_a_summary_converted_under_the_display_name_id_still_replays(tmp_path: Path) -> None:
    """The summary an Agno-layout row kept, which the conversion stores under the row's owner, moves to the agent."""
    session = _session("session", _DISPLAY_NAME_ID)
    session.summary = ScopeSummary(text="summary kept by Agno", updated_at=datetime.fromtimestamp(0, UTC))
    _agno_database(tmp_path / "code", session)

    store = create_agent_state_storage("code", tmp_path / "code")

    loaded = store.load_session("session")
    assert loaded is not None
    assert loaded.summary is not None
    assert loaded.summary.text == "summary kept by Agno"
    assert [row.summary for row in summary_rows_of(store, _SCOPE.key, "session")] == ["summary kept by Agno"]


def test_a_summary_stored_under_the_display_name_id_still_replays_after_the_upgrade(tmp_path: Path) -> None:
    """A version 2 database's summary rows under the row's old owner move to the agent, in their order."""
    session = _session("session", _DISPLAY_NAME_ID)
    session.runs.append(_run("later"))
    stored = create_agent_state_storage("code", tmp_path / "code")
    seed_session(stored, session)
    loaded = stored.load_session("session")
    assert loaded is not None
    # The rows go under the old owner's scope, where a version 2 database whose row named it stored them.
    under_old_owner = replace(loaded, owner_id=_DISPLAY_NAME_ID)
    compact_runs(stored, under_old_owner, ["session-run"], "first summary")
    compact_runs(stored, under_old_owner, ["later"], "second summary")
    _as_layout_2(stored, session)

    store = create_agent_state_storage("code", tmp_path / "code")

    reloaded = store.load_session("session")
    assert reloaded is not None
    assert reloaded.summary is not None
    assert reloaded.summary.text == "second summary"
    assert [run.run_id for run in reloaded.runs if replays_in_scope(run, _SCOPE)] == []
    assert [row.summary for row in summary_rows_of(store, _SCOPE.key, "session")] == [
        "first summary",
        "second summary",
    ]
